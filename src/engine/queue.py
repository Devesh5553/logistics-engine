"""
queue.py — Phase 3: /queue endpoint backing logic.

For a given actor, produces the list of jobs they *can* act on right now,
grouped by the next eligible action.  The query is intentional and cheap:

  1. Resolve the user's active, in-scope grants → collect (client, branch,
     direction, mode) tuples they cover.
  2. For each action the user's roles permit, determine the stage(s) where
     that action is legal from STAGE_ALLOWED_ACTIONS.
  3. Pull jobs matching any of those (scope, stage) combinations in one
     single SQL IN query.
  4. For each candidate job, run a lightweight decide() call (no audit log
     write) to confirm the user can really act on it.  This ensures that
     separation-of-duties and dual-control checks are applied without
     duplicating logic.

Returns a QueueResult Pydantic model ready for the API layer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict

from sqlalchemy.orm import Session
from sqlalchemy import or_, and_

from src.models import User, Grant, Job, Assignment
from src.engine.evaluator import decide, to_naive_utc, STAGE_ALLOWED_ACTIONS
from src.engine.types import DecisionResult

logger = logging.getLogger("access_control.queue")


# ---------------------------------------------------------------------------
# Pydantic response models for the queue
# ---------------------------------------------------------------------------
from pydantic import BaseModel, Field


class QueueJobItem(BaseModel):
    job_id: str
    client_id: str
    branch_id: str
    direction: str
    mode: str
    stage: str
    title: Optional[str] = None
    eligible_actions: List[str] = Field(default_factory=list)
    needs_reassignment: bool = False
    reassignment_reason: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)


class QueueResult(BaseModel):
    user_id: str
    username: str
    full_name: str
    at: str
    total_jobs: int
    items: List[QueueJobItem] = Field(default_factory=list)
    is_absent: bool = False
    absence_note: Optional[str] = None


# ---------------------------------------------------------------------------
# Action → required stages mapping (derived from STAGE_ALLOWED_ACTIONS)
# ---------------------------------------------------------------------------
def _build_action_to_stages() -> Dict[str, List[str]]:
    """Inverts STAGE_ALLOWED_ACTIONS: action → list of stages where it is legal."""
    action_stages: Dict[str, List[str]] = {}
    for stage, actions in STAGE_ALLOWED_ACTIONS.items():
        for action in actions:
            action_stages.setdefault(action, []).append(stage)
    return action_stages


ACTION_TO_STAGES = _build_action_to_stages()


# ---------------------------------------------------------------------------
# Core queue builder
# ---------------------------------------------------------------------------
def build_queue(
    user_or_id,
    session: Session,
    at: Optional[datetime] = None,
    action_filter: Optional[str] = None,
    branch_filter: Optional[str] = None,
    client_filter: Optional[str] = None,
    limit: Optional[int] = 200,
) -> QueueResult:
    """
    Build the actionable job queue for `user_or_id` at timestamp `at`.

    Parameters
    ----------
    user_or_id : User | str
        User object or user ID/username.
    session : Session
        Live DB session.
    at : datetime | None
        Evaluation timestamp (defaults to UTC now).
    action_filter : str | None
        If set, restricts output to jobs where this specific action is eligible.
    branch_filter : str | None
        Optional branch restriction.
    client_filter : str | None
        Optional client restriction.
    limit : int
        Maximum number of jobs to return (default 200).

    Returns
    -------
    QueueResult
    """
    if at is None:
        at = datetime.now(timezone.utc)

    # --- 1. Resolve user ---
    user = user_or_id
    if isinstance(user_or_id, str):
        user = (
            session.query(User)
            .filter((User.id == user_or_id) | (User.username == user_or_id))
            .first()
        )

    if user is None:
        return QueueResult(
            user_id=str(user_or_id),
            username=str(user_or_id),
            full_name="Unknown",
            at=at.isoformat(),
            total_jobs=0,
        )

    # --- 2. Short-circuit: inactive users have an empty queue ---
    if not user.is_active:
        return QueueResult(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            at=at.isoformat(),
            total_jobs=0,
        )

    # Collect absence note (shown in response but absence does NOT block the
    # queue — the user might still be able to view or cover for someone)
    absence_note = None
    is_absent = False
    if user.is_absent and user.absent_from and user.absent_to:
        at_n = to_naive_utc(at)
        af_n = to_naive_utc(user.absent_from)
        at_n2 = to_naive_utc(user.absent_to)
        if af_n and at_n2 and af_n <= at_n <= at_n2:
            is_absent = True
            absence_note = (
                f"User is on leave until "
                f"{user.absent_to.strftime('%Y-%m-%d') if user.absent_to else 'N/A'}: "
                f"{user.absent_reason or ''}"
            )

    # --- 3. Resolve active grants → scope tuples ---
    all_grants = session.query(Grant).filter(Grant.user_id == user.id).all()
    active_grants = [g for g in all_grants if g.is_active_at(at)]

    if not active_grants:
        return QueueResult(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            at=at.isoformat(),
            total_jobs=0,
            is_absent=is_absent,
            absence_note=absence_note,
        )

    # --- 4. Determine which permissions the user has ---
    from src.models import RolePermission
    in_scope_role_ids = {g.role_id for g in active_grants}
    rps = (
        session.query(RolePermission)
        .filter(RolePermission.role_id.in_(in_scope_role_ids))
        .all()
    )
    granted_permissions = {rp.permission_id for rp in rps}

    # Client users can only view — queue only shows their own jobs
    if user.user_type == "client_user":
        granted_permissions = {"job:view"}

    # Determine actionable permissions (exclude pure-view for queue purposes)
    # We show "job:view" for client users, but for staff we prefer act permissions
    act_permissions = granted_permissions - {"job:view", "job:view_internal"}
    if user.user_type == "client_user":
        act_permissions = {"job:view"}

    if action_filter:
        if action_filter not in act_permissions:
            return QueueResult(
                user_id=user.id,
                username=user.username,
                full_name=user.full_name,
                at=at.isoformat(),
                total_jobs=0,
                is_absent=is_absent,
                absence_note=absence_note,
            )
        act_permissions = {action_filter}

    # --- 5. Collect (scope, stages) combinations to query ---
    # Build the OR conditions for the SQL query
    or_clauses = []

    # For each grant, collect the stages reachable via the grant's role permissions
    for grant in active_grants:
        # Permissions this grant's role carries that we're interested in
        from src.models import RolePermission as RP
        grant_role_perm_ids = {
            rp.permission_id
            for rp in grant.role.permissions
            if rp.permission_id in act_permissions
        }
        if not grant_role_perm_ids:
            continue

        # Stages reachable by any of these permissions
        reachable_stages = set()
        for perm_id in grant_role_perm_ids:
            reachable_stages.update(ACTION_TO_STAGES.get(perm_id, []))

        if not reachable_stages:
            continue

        # Build scope predicates
        conds = [Job.stage.in_(list(reachable_stages))]

        if grant.client_id is not None:
            conds.append(Job.client_id == grant.client_id)
        elif user.user_type == "client_user" and user.client_id:
            conds.append(Job.client_id == user.client_id)

        if grant.branch_id is not None:
            conds.append(Job.branch_id == grant.branch_id)
        if grant.direction is not None:
            conds.append(Job.direction == grant.direction)
        if grant.mode is not None:
            conds.append(Job.mode == grant.mode)

        # Optional external filters
        if branch_filter:
            conds.append(Job.branch_id == branch_filter)
        if client_filter:
            conds.append(Job.client_id == client_filter)

        or_clauses.append(and_(*conds))

    if not or_clauses:
        return QueueResult(
            user_id=user.id,
            username=user.username,
            full_name=user.full_name,
            at=at.isoformat(),
            total_jobs=0,
            is_absent=is_absent,
            absence_note=absence_note,
        )

    # --- 6. Single bulk query ---
    query = session.query(Job).filter(or_(*or_clauses)).order_by(Job.created_at.desc())
    if limit is not None and limit > 0:
        candidate_jobs: List[Job] = query.limit(limit * 3).all()
    else:
        candidate_jobs: List[Job] = query.all()

    # --- 7. Per-job decide() verification + build QueueJobItem ---
    items: List[QueueJobItem] = []
    for job in candidate_jobs:
        if limit is not None and limit > 0 and len(items) >= limit:
            break

        eligible_actions = []
        for perm_id in act_permissions:
            # Quick decide() with no audit logging for queue scan
            result = decide(
                user_or_id=user,
                action=perm_id,
                job_or_id=job,
                at=at,
                session=session,
                log_denials=False,
            )
            if result.allowed:
                eligible_actions.append(perm_id)

        if not eligible_actions:
            continue

        # Reassignment warnings from assignments table
        flagged_assignments = [
            a for a in job.assignments
            if a.needs_reassignment and a.is_active
        ]
        needs_reassignment = bool(flagged_assignments)
        reassignment_reason = (
            flagged_assignments[0].reassignment_reason if flagged_assignments else None
        )

        warnings = []
        if needs_reassignment:
            warnings.append(f"Job needs reassignment: {reassignment_reason or 'assigned user unavailable'}")

        items.append(
            QueueJobItem(
                job_id=job.id,
                client_id=job.client_id,
                branch_id=job.branch_id,
                direction=job.direction,
                mode=job.mode,
                stage=job.stage,
                title=job.title,
                eligible_actions=sorted(eligible_actions),
                needs_reassignment=needs_reassignment,
                reassignment_reason=reassignment_reason,
                warnings=warnings,
            )
        )

    return QueueResult(
        user_id=user.id,
        username=user.username,
        full_name=user.full_name,
        at=at.isoformat(),
        total_jobs=len(items),
        items=items,
        is_absent=is_absent,
        absence_note=absence_note,
    )
