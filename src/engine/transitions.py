"""
transitions.py — Phase 3: Workflow state machine transition executor.

Handles the `POST /jobs/{job_id}/transition` endpoint logic.  For each
allowed workflow edge, this module:

  1. Calls `decide()` to confirm the actor has permission.
  2. Validates the requested action maps to the job's current stage.
  3. Applies the state mutation (stage advance, field updates).
  4. Appends a JobEvent for the history trail.
  5. Returns a structured TransitionResult (success or denial).

Stage transitions allowed by the workflow:
  Confirmed   → Prepared    (job:prepare)
  Prepared    → Checked     (job:check)
  Checked     → Filed       (job:file)
  Filed       → Cancelled   (job:cancel_filed)   ← dual-control required
  Filed       → Reopened    (job:reopen_closed)   ← dual-control required
  Cancelled   → Reopened    (job:reopen_closed)   ← dual-control required
  Reopened    → Prepared    (job:prepare)
  Reopened    → Checked     (job:check)
  Reopened    → Filed       (job:file)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from src.models import User, Job, JobEvent, Approval
from src.engine.evaluator import decide, to_naive_utc

logger = logging.getLogger("access_control.transitions")


# ---------------------------------------------------------------------------
# Pydantic response model
# ---------------------------------------------------------------------------
class TransitionResult(BaseModel):
    success: bool
    job_id: str
    from_stage: Optional[str] = None
    to_stage: Optional[str] = None
    action: str
    actor_id: Optional[str] = None
    deny_code: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)
    rule: Optional[str] = None
    event_id: Optional[str] = None
    message: str = ""


# ---------------------------------------------------------------------------
# Stage graph
# ---------------------------------------------------------------------------
# Maps (action, from_stage) → to_stage
TRANSITION_MAP: dict[tuple[str, str], str] = {
    ("job:prepare", "Confirmed"): "Prepared",
    ("job:prepare", "Reopened"): "Prepared",
    ("job:check", "Prepared"): "Checked",
    ("job:check", "Reopened"): "Checked",
    ("job:file", "Checked"): "Filed",
    ("job:file", "Reopened"): "Filed",
    ("job:cancel_filed", "Filed"): "Cancelled",
    ("job:reopen_closed", "Filed"): "Reopened",
    ("job:reopen_closed", "Cancelled"): "Reopened",
}

# Audit action name for the JobEvent record
ACTION_EVENT_NAME: dict[str, str] = {
    "job:prepare": "PREPARE",
    "job:check": "CHECK",
    "job:file": "FILE",
    "job:cancel_filed": "CANCEL",
    "job:reopen_closed": "REOPEN",
}


# ---------------------------------------------------------------------------
# Transition executor
# ---------------------------------------------------------------------------
def execute_transition(
    user_or_id,
    job_or_id,
    action: str,
    session: Session,
    reason: Optional[str] = None,
    at: Optional[datetime] = None,
    expected_version: Optional[int] = None,
    approval_id: Optional[str] = None,
) -> TransitionResult:
    """
    Attempt to transition `job` from its current stage via `action`.
    Authorization happens BEFORE version check so unauthorized callers learn nothing.
    """
    if at is None:
        at = datetime.now(timezone.utc)

    # --- Resolve objects ---
    user = user_or_id
    if isinstance(user_or_id, str):
        user = (
            session.query(User)
            .filter((User.id == user_or_id) | (User.username == user_or_id))
            .first()
        )

    job = job_or_id
    if isinstance(job_or_id, str):
        job = session.query(Job).filter(Job.id == job_or_id).first()

    if job is None:
        return TransitionResult(
            success=False,
            job_id=str(job_or_id),
            action=action,
            deny_code="NOT_FOUND",
            reasons=["Job not found."],
            rule="EXISTENCE_CHECK",
            message="Job not found.",
        )

    if user is None:
        return TransitionResult(
            success=False,
            job_id=job.id,
            action=action,
            deny_code="USER_NOT_FOUND",
            reasons=["User not found."],
            rule="EXISTENCE_CHECK",
            message="User not found.",
        )

    # --- High-risk actions require a reason ---
    HIGH_RISK_ACTIONS = {"job:cancel_filed", "job:reopen_closed", "job:edit_scope"}
    if action in HIGH_RISK_ACTIONS and not reason:
        return TransitionResult(
            success=False,
            job_id=job.id,
            from_stage=job.stage,
            action=action,
            actor_id=user.id,
            deny_code="REASON_REQUIRED",
            reasons=[
                f"Action '{action}' is high-risk and requires a mandatory written reason."
            ],
            rule="HIGH_RISK_MANDATORY_REASON",
            message="Mandatory reason missing for high-risk action.",
        )

    # --- Optimistic locking check ---
    if expected_version is not None and job.version != expected_version:
        return TransitionResult(
            success=False,
            job_id=job.id,
            from_stage=job.stage,
            action=action,
            actor_id=user.id,
            deny_code="VERSION_CONFLICT",
            reasons=[
                f"Job version mismatch: expected {expected_version}, but job is at version {job.version}."
            ],
            rule="OPTIMISTIC_LOCKING",
            message="Concurrent modification detected (optimistic locking).",
        )

    # --- Check if action is valid for the current stage ---
    to_stage = TRANSITION_MAP.get((action, job.stage))
    if to_stage is None:
        return TransitionResult(
            success=False,
            job_id=job.id,
            from_stage=job.stage,
            action=action,
            actor_id=user.id,
            deny_code="INVALID_TRANSITION",
            reasons=[
                f"Action '{action}' is not a valid transition from stage '{job.stage}'."
            ],
            rule="STAGE_TRANSITION_GRAPH",
            message=f"No transition defined for ({action}, {job.stage}).",
        )

    # --- Run full access check via decide() ---
    decision = decide(
        user_or_id=user,
        action=action,
        job_or_id=job,
        at=at,
        session=session,
        log_denials=True,
        approval_id=approval_id,
    )

    if not decision.allowed:
        return TransitionResult(
            success=False,
            job_id=job.id,
            from_stage=job.stage,
            action=action,
            actor_id=user.id,
            deny_code=decision.deny_code,
            reasons=decision.reasons,
            rule=decision.rule,
            message=f"Access denied: {decision.deny_code}",
        )

    # --- Apply state mutation ---
    from_stage = job.stage
    old_snapshot = {
        "stage": from_stage,
        "version": job.version,
        "prepared_by": job.prepared_by,
        "checked_by": job.checked_by,
        "locked": job.locked,
    }

    job.stage = to_stage
    job.version += 1

    # Update tracking fields based on the action
    if action == "job:prepare":
        job.prepared_by = user.id
    elif action == "job:check":
        job.checked_by = user.id
    elif action == "job:file":
        job.locked = True
        job.locked_reason = f"Filed to Indian Customs ICEGATE by {user.username}"
    elif action == "job:cancel_filed":
        job.locked = True
        job.locked_reason = f"Cancelled after filing by {user.username}: {reason}"
    elif action == "job:reopen_closed":
        job.locked = False
        job.locked_reason = f"Reopened by {user.username}: {reason}"

    # Consume the exact approval validated by decide()
    if decision.approval_id:
        approval = session.query(Approval).filter(Approval.id == decision.approval_id).first()
        if approval:
            approval.used_at = to_naive_utc(at)
            approval.status = "used"

    new_snapshot = {
        "stage": to_stage,
        "version": job.version,
        "prepared_by": job.prepared_by,
        "checked_by": job.checked_by,
        "locked": job.locked,
    }

    # --- Append JobEvent ---
    event_action = ACTION_EVENT_NAME.get(action, action.upper())
    event = JobEvent(
        job_id=job.id,
        actor_id=user.id,
        action=event_action,
        from_stage=from_stage,
        to_stage=to_stage,
        reason=reason,
        old_values=json.dumps(old_snapshot),
        new_values=json.dumps(new_snapshot),
        timestamp=to_naive_utc(at),
    )
    session.add(event)
    session.flush()

    logger.info(
        f"Transition: {user.username} performed '{action}' on {job.id}: "
        f"{from_stage} → {to_stage}"
    )

    return TransitionResult(
        success=True,
        job_id=job.id,
        from_stage=from_stage,
        to_stage=to_stage,
        action=action,
        actor_id=user.id,
        event_id=event.id,
        message=f"Job {job.id} transitioned from '{from_stage}' to '{to_stage}'.",
    )
