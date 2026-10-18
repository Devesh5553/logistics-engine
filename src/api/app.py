import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional, List
from pydantic import BaseModel, Field
from fastapi import FastAPI, Depends, Query, HTTPException, status, Path, Body, Header
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from src.db import SessionLocal
from src.models import (
    User,
    Job,
    Assignment,
    Client,
    Branch,
    Grant,
    Role,
    RolePermission,
    Approval,
    JobEvent,
    AuditLog,
)
from src.engine import (
    decide,
    has_permission,
    compare_access,
    DecisionResult,
    ComparisonResult,
    build_queue,
    QueueResult,
    execute_transition,
    TransitionResult,
)
from src.engine.evaluator import to_naive_utc

app = FastAPI(
    title="Nagarkot Forwarders Pvt. Ltd. - Access Control Service",
    description=(
        "A scoped-role, explainable, audit-first access control service deciding "
        "who may do what on which customs brokerage job."
    ),
    version="3.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_at(at_str: Optional[str], param_name: str = "at") -> Optional[datetime]:
    if not at_str:
        return None
    try:
        clean_str = at_str.strip()
        if " " in clean_str and ("+" not in clean_str):
            clean_str = clean_str.replace(" ", "+")
        return datetime.fromisoformat(clean_str)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid ISO timestamp format for '{param_name}'",
        )


def get_current_actor(
    x_actor_id: Optional[str] = Header(None, alias="X-Actor-Id"),
    db: Session = Depends(get_db),
) -> User:
    """
    Extracts and authenticates caller identity strictly from the X-Actor-Id header.
    Missing header returns 401; unknown user returns 401.
    """
    if not x_actor_id or not x_actor_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required: missing X-Actor-Id header",
        )
    user = (
        db.query(User)
        .filter((User.id == x_actor_id) | (User.username == x_actor_id))
        .first()
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: unknown user '{x_actor_id}'",
        )
    return user


# ---------------------------------------------------------------------------
# Audit and Denial persistence helpers (F2)
# ---------------------------------------------------------------------------

def log_action_denial_and_raise(
    actor_id: Optional[str],
    target_type: str,
    target_id: Optional[str],
    action: str,
    deny_code: str,
    reasons: list[str],
    at: datetime,
    status_code: int = status.HTTP_403_FORBIDDEN,
    db: Optional[Session] = None,
):
    """
    Writes denial in its own committed transaction BEFORE raising HTTPException,
    guaranteeing audit log persistence even under HTTP error rollbacks.
    """
    bind = None
    if db is not None:
        try:
            bind = db.get_bind()
        except Exception:
            bind = None
    if bind is not None:
        from sqlalchemy.orm import sessionmaker
        NewSession = sessionmaker(bind=bind)
        audit_db = NewSession()
    else:
        audit_db = SessionLocal()

    try:
        last_log = audit_db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        prev_h = last_log.record_hash if last_log else "0" * 64

        rec_hash = AuditLog.compute_hash(
            prev_hash=prev_h,
            timestamp=at,
            event_type="DENIAL",
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            decision="DENY",
            deny_code=deny_code,
            reasons=json.dumps(reasons),
            grant_ids_used=None,
            permission_snapshot=None,
            details=None,
        )

        entry = AuditLog(
            timestamp=to_naive_utc(at),
            event_type="DENIAL",
            actor_id=actor_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            decision="DENY",
            deny_code=deny_code,
            reasons=json.dumps(reasons),
            prev_hash=prev_h,
            record_hash=rec_hash,
        )
        audit_db.add(entry)
        audit_db.commit()
    except Exception:
        audit_db.rollback()
    finally:
        audit_db.close()

    raise HTTPException(
        status_code=status_code,
        detail={
            "deny_code": deny_code,
            "reasons": reasons,
            "action": action,
        },
    )


def log_access_probe_and_raise_404(
    actor_id: Optional[str],
    job_id: str,
    action: str,
    at: datetime,
    db: Optional[Session] = None,
):
    """
    Logs out-of-scope or non-existent job access attempts as ACCESS_PROBE
    in an independent committed transaction while returning generic 404.
    """
    bind = None
    if db is not None:
        try:
            bind = db.get_bind()
        except Exception:
            bind = None
    if bind is not None:
        from sqlalchemy.orm import sessionmaker
        NewSession = sessionmaker(bind=bind)
        audit_db = NewSession()
    else:
        audit_db = SessionLocal()
    try:
        last_log = audit_db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        prev_h = last_log.record_hash if last_log else "0" * 64
        probe_reasons = ["Out-of-scope or non-existent job access probe."]

        rec_hash = AuditLog.compute_hash(
            prev_hash=prev_h,
            timestamp=at,
            event_type="ACCESS_PROBE",
            actor_id=actor_id,
            target_type="job",
            target_id=job_id,
            action=action,
            decision="DENY",
            deny_code="NOT_FOUND",
            reasons=json.dumps(probe_reasons),
            grant_ids_used=None,
            permission_snapshot=None,
            details=None,
        )

        entry = AuditLog(
            timestamp=to_naive_utc(at),
            event_type="ACCESS_PROBE",
            actor_id=actor_id,
            target_type="job",
            target_id=job_id,
            action=action,
            decision="DENY",
            deny_code="NOT_FOUND",
            reasons=json.dumps(probe_reasons),
            prev_hash=prev_h,
            record_hash=rec_hash,
        )
        audit_db.add(entry)
        audit_db.commit()
    except Exception:
        audit_db.rollback()
    finally:
        audit_db.close()

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Job not found",
    )


def record_audit_log(
    session: Session,
    at: datetime,
    event_type: str,
    actor_id: Optional[str],
    target_type: str,
    target_id: Optional[str],
    action: str,
    decision: str,
    deny_code: Optional[str] = None,
    reasons: Optional[list[str]] = None,
    grant_ids_used: Optional[list[str]] = None,
    permission_snapshot: Optional[dict] = None,
    details: Optional[dict] = None,
) -> AuditLog:
    last_log = session.query(AuditLog).order_by(AuditLog.id.desc()).first()
    prev_h = last_log.record_hash if last_log else "0" * 64

    reasons_str = json.dumps(reasons) if reasons else None
    grant_ids_str = json.dumps(grant_ids_used) if grant_ids_used else None
    snapshot_str = json.dumps(permission_snapshot) if permission_snapshot else None
    details_str = json.dumps(details) if details else None

    rec_hash = AuditLog.compute_hash(
        prev_hash=prev_h,
        timestamp=at,
        event_type=event_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        decision=decision,
        deny_code=deny_code,
        reasons=reasons_str,
        grant_ids_used=grant_ids_str,
        permission_snapshot=snapshot_str,
        details=details_str,
    )

    entry = AuditLog(
        timestamp=to_naive_utc(at),
        event_type=event_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        action=action,
        decision=decision,
        deny_code=deny_code,
        reasons=reasons_str,
        grant_ids_used=grant_ids_str,
        permission_snapshot=snapshot_str,
        details=details_str,
        prev_hash=prev_h,
        record_hash=rec_hash,
    )
    session.add(entry)
    session.flush()
    return entry


def get_user_permission_snapshot(db: Session, user: Optional[User], at: datetime) -> Optional[dict]:
    if not user:
        return None
    at_naive = to_naive_utc(at)
    active_grants = (
        db.query(Grant)
        .filter(
            Grant.user_id == user.id,
            Grant.valid_from <= at_naive,
            (Grant.valid_to == None) | (Grant.valid_to >= at_naive),
            (Grant.revoked_at == None) | (Grant.revoked_at >= at_naive),
        )
        .all()
    )
    roles_info = []
    for g in active_grants:
        roles_info.append({
            "grant_id": g.id,
            "role_id": g.role_id,
            "client_id": g.client_id,
            "branch_id": g.branch_id,
            "direction": g.direction,
            "mode": g.mode,
            "source": g.source,
        })
    return {
        "user_id": user.id,
        "username": user.username,
        "user_type": user.user_type,
        "active_grants": roles_info,
    }


def is_admin_level_role(db: Session, role_id: str) -> bool:
    rps = db.query(RolePermission).filter(RolePermission.role_id == role_id).all()
    perms = {rp.permission_id for rp in rps}
    return bool(perms & {"grant:manage", "admin:override", "user:manage"})


def flag_user_assignments(
    db: Session,
    user: User,
    grant: Grant,
    eval_time: datetime,
    reason: str,
) -> int:
    """
    F8: Flag an assignment only if:
      - assignment.assigned_role equals the grant's role_id,
      - the job is in the grant's scope,
      - AND the user has no other active grant of that role that still covers the job at eval_time.
    """
    other_active_grants = [
        g for g in user.grants
        if g.id != grant.id and g.role_id == grant.role_id and g.is_active_at(eval_time)
    ]
    valid_other_grants = []
    for g in other_active_grants:
        if g.source == "delegation" and g.parent_grant_id:
            parent = db.query(Grant).filter(Grant.id == g.parent_grant_id).first()
            if not parent or not parent.is_active_at(eval_time):
                continue
        valid_other_grants.append(g)

    flagged_count = 0
    for asgn in user.assigned_jobs:
        if not asgn.is_active:
            continue
        if asgn.assigned_role != grant.role_id:
            continue
        job = asgn.job
        if not job:
            continue

        c_match = (grant.client_id is None) or (grant.client_id == job.client_id)
        b_match = (grant.branch_id is None) or (grant.branch_id == job.branch_id)
        d_match = (grant.direction is None) or (grant.direction == job.direction)
        m_match = (grant.mode is None) or (grant.mode == job.mode)
        if not (c_match and b_match and d_match and m_match):
            continue

        covered_by_other = False
        for og in valid_other_grants:
            oc = (og.client_id is None) or (og.client_id == job.client_id)
            ob = (og.branch_id is None) or (og.branch_id == job.branch_id)
            od = (og.direction is None) or (og.direction == job.direction)
            om = (og.mode is None) or (og.mode == job.mode)
            if oc and ob and od and om:
                covered_by_other = True
                break

        if not covered_by_other:
            asgn.needs_reassignment = True
            asgn.reassignment_reason = reason
            flagged_count += 1

    return flagged_count


def cascade_grant_end(
    db: Session,
    grant: Grant,
    eval_time: datetime,
    actor_id: str,
    new_valid_to: Optional[datetime] = None,
    is_revocation: bool = False,
    reason: Optional[str] = None,
):
    """
    F8: When a grant is revoked or end-dated, also revoke or end every delegation
    whose parent_grant_id is that grant, with its own audit row.
    """
    child_delegations = (
        db.query(Grant)
        .filter(Grant.parent_grant_id == grant.id, Grant.revoked_at.is_(None))
        .all()
    )
    for child in child_delegations:
        if is_revocation:
            child.revoked_at = to_naive_utc(eval_time)
            child.revocation_reason = f"Cascaded revocation from parent grant {grant.id}: {reason or ''}"
            record_audit_log(
                session=db,
                at=eval_time,
                event_type="GRANT_CHANGE",
                actor_id=actor_id,
                target_type="grant",
                target_id=child.id,
                action="GRANT_REVOKE",
                decision="EXECUTED",
                reasons=[f"Cascaded revocation from parent grant {grant.id}"],
            )
            if child.user:
                flag_user_assignments(db, child.user, child, eval_time, f"Parent grant revoked: {grant.id}")
        elif new_valid_to:
            c_vt = to_naive_utc(child.valid_to)
            n_vt = to_naive_utc(new_valid_to)
            if c_vt is None or n_vt < c_vt:
                child.valid_to = n_vt
                record_audit_log(
                    session=db,
                    at=eval_time,
                    event_type="GRANT_CHANGE",
                    actor_id=actor_id,
                    target_type="grant",
                    target_id=child.id,
                    action="GRANT_END_DATE",
                    decision="EXECUTED",
                    reasons=[f"Cascaded end-date from parent grant {grant.id}"],
                )
                if n_vt <= to_naive_utc(eval_time) and child.user:
                    flag_user_assignments(db, child.user, child, eval_time, f"Parent grant ended: {grant.id}")


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class JobDetailResponse(BaseModel):
    job_id: str
    client_id: str
    client_name: Optional[str] = None
    branch_id: str
    branch_name: Optional[str] = None
    direction: str
    mode: str
    stage: str
    title: Optional[str] = None
    goods_description: Optional[str] = None
    prepared_by: Optional[str] = None
    checked_by: Optional[str] = None
    version: int
    locked: bool
    locked_reason: Optional[str] = None
    created_at: str
    updated_at: str
    test_case_note: Optional[str] = None


class AssignmentItem(BaseModel):
    assignment_id: str
    job_id: str
    user_id: str
    username: Optional[str] = None
    full_name: Optional[str] = None
    assigned_role: str
    is_active: bool
    needs_reassignment: bool
    reassignment_reason: Optional[str] = None
    assigned_at: str


class AssignmentRequest(BaseModel):
    assignee_id: str = Field(..., description="ID or username of the user being assigned")
    role: str = Field(..., description="Role to assign: 'Maker' or 'Checker'")
    at: Optional[str] = Field(None, description="ISO-8601 evaluation timestamp")


class TransitionRequest(BaseModel):
    action: str = Field(..., description="Action to perform (e.g. 'job:prepare', 'job:check')")
    version: Optional[int] = Field(None, description="Expected job version for optimistic locking (mandatory)")
    reason: Optional[str] = Field(None, description="Mandatory reason for high-risk actions")
    approval_id: Optional[str] = Field(None, description="Pre-authorized approval ID")
    at: Optional[str] = Field(None, description="ISO-8601 evaluation timestamp")


class CreateGrantRequest(BaseModel):
    user_id: str = Field(..., description="Target user receiving the grant")
    role_id: str = Field(..., description="Role to grant (must exist in configuration)")
    client_id: Optional[str] = None
    branch_id: Optional[str] = None
    direction: Optional[str] = None
    mode: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    source: Optional[str] = "direct"
    parent_grant_id: Optional[str] = None
    reason: Optional[str] = None
    approval_id: Optional[str] = None
    at: Optional[str] = None


class GrantResponse(BaseModel):
    id: str
    user_id: str
    role_id: str
    client_id: Optional[str] = None
    branch_id: Optional[str] = None
    direction: Optional[str] = None
    mode: Optional[str] = None
    valid_from: str
    valid_to: Optional[str] = None
    revoked_at: Optional[str] = None
    revocation_reason: Optional[str] = None
    granted_by: Optional[str] = None
    approved_by: Optional[str] = None
    source: str
    parent_grant_id: Optional[str] = None
    created_at: str


class EndDateGrantRequest(BaseModel):
    valid_to: str = Field(..., description="New valid_to timestamp (ISO-8601)")
    reason: Optional[str] = None
    approval_id: Optional[str] = None
    at: Optional[str] = None


class RevokeGrantRequest(BaseModel):
    reason: str = Field(..., description="Mandatory reason for revoking the grant")
    approval_id: Optional[str] = None
    at: Optional[str] = None


class AdminOverrideRequest(BaseModel):
    action: str = Field("admin:override", description="Override action")
    reason: str = Field(..., description="Mandatory written reason for override")
    target_stage: Optional[str] = None
    unlock: Optional[bool] = None
    approval_id: Optional[str] = None
    at: Optional[str] = None


class ScopeEditRequest(BaseModel):
    reason: str = Field(..., description="Mandatory written reason for scope change")
    expected_version: Optional[int] = Field(None, description="Expected job version for optimistic locking")
    client_id: Optional[str] = None
    branch_id: Optional[str] = None
    direction: Optional[str] = None
    mode: Optional[str] = None
    approval_id: Optional[str] = None
    at: Optional[str] = None


class DeactivateUserRequest(BaseModel):
    reason: Optional[str] = "Employee leaver / departure"
    at: Optional[str] = None


class TransferUserRequest(BaseModel):
    from_branch_id: str = Field(..., description="Current branch ID to transfer from")
    to_branch_id: str = Field(..., description="New branch ID to transfer to")
    reason: Optional[str] = "Branch transfer"
    at: Optional[str] = None


class ApprovalRequest(BaseModel):
    action: str = Field(..., description="Action being requested")
    target_type: str = Field(..., description="Target type ('job', 'grant', 'user')")
    target_id: str = Field(..., description="Target entity ID")
    reason: str = Field(..., description="Mandatory written justification for the request")
    at: Optional[str] = None


class ApprovalResponse(BaseModel):
    id: str
    action: Optional[str] = None
    target_type: str
    target_id: str
    requested_by: str
    approved_by: Optional[str] = None
    reason: str
    status: str
    approved_at: Optional[str] = None
    expires_at: str
    used_at: Optional[str] = None
    created_at: str


def _approval_to_response(a: Approval) -> ApprovalResponse:
    return ApprovalResponse(
        id=a.id,
        action=a.action,
        target_type=a.target_type,
        target_id=a.target_id,
        requested_by=a.requested_by,
        approved_by=a.approved_by,
        reason=a.reason,
        status=a.status,
        approved_at=a.approved_at.isoformat() if a.approved_at else None,
        expires_at=a.expires_at.isoformat() if a.expires_at else "",
        used_at=a.used_at.isoformat() if a.used_at else None,
        created_at=a.created_at.isoformat() if a.created_at else "",
    )


def _grant_to_response(g: Grant) -> GrantResponse:
    return GrantResponse(
        id=g.id,
        user_id=g.user_id,
        role_id=g.role_id,
        client_id=g.client_id,
        branch_id=g.branch_id,
        direction=g.direction,
        mode=g.mode,
        valid_from=g.valid_from.isoformat() if g.valid_from else "",
        valid_to=g.valid_to.isoformat() if g.valid_to else None,
        revoked_at=g.revoked_at.isoformat() if g.revoked_at else None,
        revocation_reason=g.revocation_reason,
        granted_by=g.granted_by,
        approved_by=g.approved_by,
        source=g.source,
        parent_grant_id=g.parent_grant_id,
        created_at=g.created_at.isoformat() if g.created_at else "",
    )


def _resolve_user(user_id: str, db: Session) -> User:
    user = (
        db.query(User)
        .filter((User.id == user_id) | (User.username == user_id))
        .first()
    )
    if user is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return user


def _resolve_job(job_id: str, db: Session) -> Job:
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "healthy", "service": "NFPL Access Control"}


@app.get(
    "/can",
    response_model=DecisionResult,
    tags=["Decision Engine"],
    summary="Evaluate if a user may perform an action on a job",
)
def check_can(
    user_id: str = Query(..., description="User ID or username to evaluate"),
    action: str = Query(..., description="Action to perform (e.g. 'job:prepare', 'job:check', 'job:view')"),
    job_id: str = Query(..., description="Target Job ID (e.g. 'JOB-PLANTED-01')"),
    at: Optional[str] = Query(None, description="ISO-8601 evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at)

    # Dry run: log_denials=False. Explanation tier determined strictly by caller's audit:read
    result = decide(
        user_or_id=user_id,
        action=action,
        job_or_id=job_id,
        at=eval_time,
        session=db,
        requester_or_id=actor,
        log_denials=False,
    )

    if result.deny_code == "NOT_FOUND":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    return result


@app.get(
    "/compare",
    response_model=ComparisonResult,
    tags=["Decision Engine"],
    summary="Compare two users attempting the same action on a job",
)
def compare_users(
    user_a: str = Query(..., description="First User ID or username"),
    user_b: str = Query(..., description="Second User ID or username"),
    action: str = Query(..., description="Action to evaluate (e.g. 'job:check')"),
    job_id: str = Query(..., description="Target Job ID"),
    at: Optional[str] = Query(None, description="ISO-8601 evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at)

    try:
        # Dry run: log_denials=False. Tier determined by actor's audit:read
        comparison = compare_access(
            user_a_or_id=user_a,
            user_b_or_id=user_b,
            action=action,
            job_or_id=job_id,
            at=eval_time,
            session=db,
            requester_or_id=actor,
        )
        return comparison
    except ValueError as e:
        if "Job not found" in str(e):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.get(
    "/users/{user_id}/queue",
    response_model=QueueResult,
    tags=["Queue"],
    summary="Get the actionable job queue for a user",
)
def get_user_queue(
    user_id: str = Path(..., description="User ID or username to build queue for"),
    at: Optional[str] = Query(None, description="ISO-8601 evaluation timestamp"),
    action: Optional[str] = Query(None, description="Filter to a specific action"),
    branch: Optional[str] = Query(None, description="Filter to a specific branch code"),
    client: Optional[str] = Query(None, description="Filter to a specific client ID"),
    limit: Optional[int] = Query(200, ge=1, le=1000, description="Maximum number of jobs to return"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at)
    target_user = _resolve_user(user_id, db)

    # F5: Caller may read only their own queue unless they hold audit:read
    if actor.id != target_user.id and actor.username != target_user.username:
        if not has_permission(db, actor, "audit:read", eval_time):
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="user",
                target_id=target_user.id,
                action="queue:view",
                deny_code="ACCESS_DENIED",
                reasons=["Callers may read only their own queue unless they hold audit:read permission."],
                at=eval_time or datetime.now(timezone.utc),
                status_code=status.HTTP_403_FORBIDDEN,
            )

    result = build_queue(
        user_or_id=target_user,
        session=db,
        at=eval_time,
        action_filter=action,
        branch_filter=branch,
        client_filter=client,
        limit=limit,
    )
    return result


@app.get(
    "/queue",
    response_model=QueueResult,
    tags=["Queue"],
    summary="Get the actionable job queue for a user (query param alias)",
)
def get_queue(
    user_id: Optional[str] = Query(None, description="User ID or username"),
    at: Optional[str] = Query(None, description="ISO-8601 evaluation timestamp"),
    action: Optional[str] = Query(None, description="Filter to a specific action"),
    branch: Optional[str] = Query(None, description="Filter to a specific branch code"),
    client: Optional[str] = Query(None, description="Filter to a specific client ID"),
    limit: Optional[int] = Query(200, ge=1, le=1000, description="Maximum number of jobs to return"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    target_id = user_id or actor.id
    return get_user_queue(
        user_id=target_id,
        at=at,
        action=action,
        branch=branch,
        client=client,
        limit=limit,
        actor=actor,
        db=db,
    )


@app.get(
    "/jobs/{job_id}",
    response_model=JobDetailResponse,
    tags=["Jobs"],
    summary="View a single job",
)
def get_job(
    job_id: str = Path(..., description="Job ID"),
    at: Optional[str] = Query(None, description="ISO-8601 evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    job = db.query(Job).filter(Job.id == job_id).first()

    if job is None:
        log_access_probe_and_raise_404(actor.id, job_id, "job:view", eval_time)

    # Decide job:view
    decision = decide(
        user_or_id=actor,
        action="job:view",
        job_or_id=job,
        at=eval_time,
        session=db,
        log_denials=False,
    )
    if not decision.allowed:
        # F2/F5: Log ACCESS_PROBE and return identical 404
        log_access_probe_and_raise_404(actor.id, job.id, "job:view", eval_time)

    job_scope = {
        "client_id": job.client_id,
        "branch_id": job.branch_id,
        "direction": job.direction,
        "mode": job.mode,
    }
    has_internal = has_permission(db, actor, "job:view_internal", eval_time, scope=job_scope)
    demo_mode = os.environ.get("DEMO_MODE", "").lower() in ("true", "1")

    client_obj = db.query(Client).filter(Client.id == job.client_id).first()
    branch_obj = db.query(Branch).filter(Branch.id == job.branch_id).first()

    preparer_name = None
    checker_name = None
    if has_internal:
        if job.prepared_by:
            p = db.query(User).filter(User.id == job.prepared_by).first()
            preparer_name = p.username if p else job.prepared_by
        if job.checked_by:
            c = db.query(User).filter(User.id == job.checked_by).first()
            checker_name = c.username if c else job.checked_by

    return JobDetailResponse(
        job_id=job.id,
        client_id=job.client_id,
        client_name=client_obj.name if client_obj else None,
        branch_id=job.branch_id,
        branch_name=branch_obj.name if branch_obj else None,
        direction=job.direction,
        mode=job.mode,
        stage=job.stage,
        title=job.title,
        goods_description=job.goods_description,
        prepared_by=preparer_name,
        checked_by=checker_name,
        version=job.version,
        locked=job.locked,
        locked_reason=job.locked_reason if has_internal else None,
        created_at=job.created_at.isoformat() if job.created_at else "",
        updated_at=job.updated_at.isoformat() if job.updated_at else "",
        test_case_note=job.test_case_note if (has_internal and demo_mode) else None,
    )


@app.get(
    "/jobs/{job_id}/assignments",
    response_model=List[AssignmentItem],
    tags=["Assignments"],
    summary="List all assignments for a job",
)
def list_assignments(
    job_id: str = Path(..., description="Job ID"),
    active_only: bool = Query(True, description="If true, return only active assignments"),
    at: Optional[str] = Query(None, description="ISO-8601 evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        log_access_probe_and_raise_404(actor.id, job_id, "job:view", eval_time)

    decision = decide(
        user_or_id=actor,
        action="job:view",
        job_or_id=job,
        at=eval_time,
        session=db,
        log_denials=False,
    )
    if not decision.allowed:
        log_access_probe_and_raise_404(actor.id, job.id, "job:view", eval_time)

    job_scope = {
        "client_id": job.client_id,
        "branch_id": job.branch_id,
        "direction": job.direction,
        "mode": job.mode,
    }
    has_internal = has_permission(db, actor, "job:view_internal", eval_time, scope=job_scope)

    q = db.query(Assignment).filter(Assignment.job_id == job_id)
    if active_only:
        q = q.filter(Assignment.is_active == True)

    assignments = q.all()
    items = []
    for asgn in assignments:
        user = db.query(User).filter(User.id == asgn.user_id).first()
        items.append(AssignmentItem(
            assignment_id=asgn.id,
            job_id=asgn.job_id,
            user_id=asgn.user_id,
            username=user.username if (user and has_internal) else None,
            full_name=user.full_name if (user and has_internal) else None,
            assigned_role=asgn.assigned_role,
            is_active=asgn.is_active,
            needs_reassignment=asgn.needs_reassignment,
            reassignment_reason=asgn.reassignment_reason,
            assigned_at=asgn.assigned_at.isoformat() if asgn.assigned_at else "",
        ))
    return items


@app.post(
    "/jobs/{job_id}/assignments",
    response_model=AssignmentItem,
    tags=["Assignments"],
    summary="Assign a user to a job",
)
def create_assignment(
    job_id: str = Path(..., description="Job ID"),
    body: AssignmentRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)
    job = _resolve_job(job_id, db)
    assignee = _resolve_user(body.assignee_id, db)

    # Assigner is derived from header
    assign_check = decide(
        user_or_id=actor,
        action="job:assign",
        job_or_id=job,
        at=eval_time,
        session=db,
        log_denials=False,
    )
    if not assign_check.allowed:
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="job",
            target_id=job.id,
            action="job:assign",
            deny_code=assign_check.deny_code,
            reasons=assign_check.reasons,
            at=eval_time,
            status_code=403,
        )

    valid_roles = {"Maker", "Checker", "Filer"}
    if body.role not in valid_roles:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid role '{body.role}'. Must be one of: {sorted(valid_roles)}",
        )

    # Deactivate existing assignment for this role on this job
    existing = (
        db.query(Assignment)
        .filter(
            Assignment.job_id == job_id,
            Assignment.assigned_role == body.role,
            Assignment.is_active == True,
        )
        .first()
    )
    if existing:
        existing.is_active = False

    new_asgn = Assignment(
        job_id=job.id,
        user_id=assignee.id,
        assigned_role=body.role,
        is_active=True,
        needs_reassignment=False,
        assigned_at=to_naive_utc(eval_time),
    )
    db.add(new_asgn)
    db.flush()
    db.commit()

    return AssignmentItem(
        assignment_id=new_asgn.id,
        job_id=new_asgn.job_id,
        user_id=new_asgn.user_id,
        username=assignee.username,
        full_name=assignee.full_name,
        assigned_role=new_asgn.assigned_role,
        is_active=new_asgn.is_active,
        needs_reassignment=new_asgn.needs_reassignment,
        reassignment_reason=new_asgn.reassignment_reason,
        assigned_at=new_asgn.assigned_at.isoformat() if new_asgn.assigned_at else "",
    )


@app.post(
    "/jobs/{job_id}/transition",
    response_model=TransitionResult,
    tags=["Jobs"],
    summary="Advance a job through its workflow stages",
)
def transition_job(
    job_id: str = Path(..., description="Job ID"),
    body: TransitionRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    actor_id = actor.id
    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)

    # F6: expected_version is required (422 when missing)
    if body.version is None:
        raise HTTPException(
            status_code=422,
            detail="expected_version is required on transitions",
        )

    result = execute_transition(
        user_or_id=actor,
        job_or_id=job_id,
        action=body.action,
        session=db,
        reason=body.reason,
        at=eval_time,
        expected_version=body.version,
        approval_id=body.approval_id,
    )

    if not result.success:
        if result.deny_code in ("NOT_FOUND", "USER_NOT_FOUND"):
            log_access_probe_and_raise_404(actor_id, job_id, body.action, eval_time, db=db)
        if result.deny_code == "VERSION_REQUIRED":
            raise HTTPException(status_code=422, detail=result.reasons[0])
        if result.deny_code == "VERSION_CONFLICT":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.reasons[0])
        if result.deny_code in ("INVALID_TRANSITION", "REASON_REQUIRED"):
            raise HTTPException(status_code=422, detail=result.reasons[0])

        # F2: Action denial must persist in its own committed transaction
        log_action_denial_and_raise(
            actor_id=actor_id,
            target_type="job",
            target_id=job_id,
            action=body.action,
            deny_code=result.deny_code,
            reasons=result.reasons,
            at=eval_time,
            status_code=403,
            db=db,
        )

    # Phase 4: Decision snapshots - record audit log for successful stage transition
    record_audit_log(
        session=db,
        at=eval_time,
        event_type="STAGE_TRANSITION",
        actor_id=actor_id,
        target_type="job",
        target_id=job_id,
        action=body.action,
        decision="EXECUTED",
        reasons=[body.reason] if body.reason else [f"Job transitioned from {result.from_stage} to {result.to_stage}"],
        permission_snapshot=get_user_permission_snapshot(db, actor, eval_time),
        details={"from_stage": result.from_stage, "to_stage": result.to_stage, "version": body.version},
    )

    db.commit()
    return result


@app.post(
    "/grants",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Grants"],
    summary="Create a new grant or delegation",
)
def create_grant(
    body: CreateGrantRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)

    # F1: source must be in allowlist {"direct", "delegation"}. Any other value returns 422
    if body.source not in {"direct", "delegation"}:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid grant source '{body.source}'. Allowed sources: direct, delegation.",
        )

    target_user = _resolve_user(body.user_id, db)

    # Roles can be added through configuration only
    role_obj = db.query(Role).filter(Role.id == body.role_id).first()
    if not role_obj:
        raise HTTPException(
            status_code=422,
            detail=f"Role '{body.role_id}' does not exist in configuration.",
        )

    # F1: valid_to must be later than valid_from
    val_from = _parse_at(body.valid_from) if body.valid_from else None
    val_to = _parse_at(body.valid_to) if body.valid_to else None
    if val_from and val_to and val_to <= val_from:
        raise HTTPException(
            status_code=422,
            detail="valid_to must be later than valid_from.",
        )

    is_admin_role = is_admin_level_role(db, body.role_id)
    approved_by_id = None
    parent_grant = None

    if body.source == "direct":
        # Direct path: Granter needs grant:manage permission (F4/F5)
        if not has_permission(db, actor, "grant:manage", eval_time):
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=None,
                action="grant:manage",
                deny_code="PERMISSION_DENIED",
                reasons=["Granter requires grant:manage permission to issue direct grants."],
                at=eval_time,
                status_code=403,
            )

    elif body.source == "delegation":
        # F1: Delegating any role that carries grant:manage, admin:override, or user:manage is forbidden
        if is_admin_role:
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_OF_ADMIN_PROHIBITED: Administrative roles cannot be delegated.",
            )

        if not body.valid_to:
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_TIMEBOX_REQUIRED: Delegation must be time-boxed with an explicit valid_to expiry.",
            )

        # Parent grant check:
        # (a) belong to granter, (b) active at evaluation time, (c) same role_id, (d) fully contain scope
        if body.parent_grant_id:
            parent_grant = db.query(Grant).filter(Grant.id == body.parent_grant_id).first()
            if parent_grant and not body.at and parent_grant.valid_from:
                eval_time = parent_grant.valid_from
        else:
            parent_grant = (
                db.query(Grant)
                .filter(
                    Grant.user_id == actor.id,
                    Grant.role_id == body.role_id,
                )
                .all()
            )
            parent_grant = next((g for g in parent_grant if g.is_active_at(eval_time)), None)

        if not parent_grant:
            raise HTTPException(
                status_code=422,
                detail="No parent grant found for delegation.",
            )

        if parent_grant.user_id != actor.id:
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_PARENT_MISMATCH: Parent grant must belong to the delegator.",
            )

        if not parent_grant.is_active_at(eval_time):
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_PARENT_INACTIVE: Parent grant is inactive or expired at evaluation time.",
            )

        if parent_grant.role_id != body.role_id:
            raise HTTPException(
                status_code=422,
                detail=f"DELEGATION_ROLE_MISMATCH: Parent grant role '{parent_grant.role_id}' does not match requested role '{body.role_id}'.",
            )

        # Scope containment check (NULL in parent means all; NULL in child where parent restricts = exceeding)
        if parent_grant.client_id is not None and (body.client_id is None or body.client_id != parent_grant.client_id):
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_SCOPE_EXCEEDED: Delegate client scope exceeds parent grant.",
            )
        if parent_grant.branch_id is not None and (body.branch_id is None or body.branch_id != parent_grant.branch_id):
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_SCOPE_EXCEEDED: Delegate branch scope exceeds parent grant.",
            )
        if parent_grant.direction is not None and (body.direction is None or body.direction != parent_grant.direction):
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_SCOPE_EXCEEDED: Delegate direction scope exceeds parent grant.",
            )
        if parent_grant.mode is not None and (body.mode is None or body.mode != parent_grant.mode):
            raise HTTPException(
                status_code=422,
                detail="DELEGATION_SCOPE_EXCEEDED: Delegate mode scope exceeds parent grant.",
            )

        # Expiry cannot exceed parent
        if parent_grant.valid_to is not None:
            p_vt = to_naive_utc(parent_grant.valid_to)
            c_vt = to_naive_utc(val_to)
            if c_vt and p_vt and c_vt > p_vt:
                raise HTTPException(
                    status_code=422,
                    detail="DELEGATION_EXPIRY_EXCEEDED: Delegate expiry cannot exceed parent grant expiry.",
                )

    # Dual control rule for admin-level roles on EVERY path
    if is_admin_role:
        # Self-grant prohibited
        if actor.id == target_user.id or actor.username == target_user.username:
            raise HTTPException(
                status_code=422,
                detail="ADMIN_SELF_GRANT_PROHIBITED: Admins cannot grant to themselves.",
            )

        if not body.approval_id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=target_user.id,
                action="grant:create",
                deny_code="DUAL_CONTROL_APPROVAL_REQUIRED",
                reasons=["Creating an admin-level grant requires a second admin's approval."],
                at=eval_time,
                status_code=403,
            )

        appr = db.query(Approval).filter(Approval.id == body.approval_id).first()
        if not appr or appr.status != "approved" or appr.used_at is not None:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=target_user.id,
                action="grant:create",
                deny_code="APPROVAL_INVALID",
                reasons=["Dual control approval token is invalid, not approved, or already used."],
                at=eval_time,
                status_code=403,
            )

        if appr.approved_by is None or appr.approved_by in (actor.id, target_user.id):
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=target_user.id,
                action="grant:create",
                deny_code="SELF_APPROVAL_PROHIBITED",
                reasons=["Approval must be signed by an independent second admin."],
                at=eval_time,
                status_code=403,
            )

        approver_user = db.query(User).filter(User.id == appr.approved_by).first()
        if not approver_user or not has_permission(db, approver_user, "grant:manage", eval_time):
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=target_user.id,
                action="grant:create",
                deny_code="APPROVER_NOT_AUTHORIZED",
                reasons=["Approver must hold grant:manage authority at evaluation time."],
                at=eval_time,
                status_code=403,
            )

        appr.used_at = to_naive_utc(eval_time)
        appr.status = "used"
        approved_by_id = appr.approved_by

    new_grant = Grant(
        user_id=target_user.id,
        role_id=body.role_id,
        client_id=body.client_id,
        branch_id=body.branch_id,
        direction=body.direction,
        mode=body.mode,
        valid_from=to_naive_utc(val_from),
        valid_to=to_naive_utc(val_to),
        source=body.source,
        parent_grant_id=parent_grant.id if (body.source == "delegation" and parent_grant) else body.parent_grant_id,
        granted_by=actor.id,
        approved_by=approved_by_id,
    )
    db.add(new_grant)
    db.flush()

    record_audit_log(
        session=db,
        at=eval_time,
        event_type="GRANT_CHANGE",
        actor_id=actor.id,
        target_type="grant",
        target_id=new_grant.id,
        action="GRANT_CREATE",
        decision="EXECUTED",
        details={"role_id": new_grant.role_id, "user_id": target_user.id, "source": new_grant.source},
    )
    db.commit()
    return _grant_to_response(new_grant)


@app.patch(
    "/grants/{grant_id}",
    response_model=GrantResponse,
    tags=["Grants"],
    summary="End-date an existing grant",
)
def end_date_grant(
    grant_id: str = Path(..., description="Grant ID"),
    body: EndDateGrantRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)
    grant = db.query(Grant).filter(Grant.id == grant_id).first()
    if not grant:
        raise HTTPException(status_code=404, detail="Grant not found")

    if not has_permission(db, actor, "grant:manage", eval_time):
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="grant",
            target_id=grant.id,
            action="grant:manage",
            deny_code="PERMISSION_DENIED",
            reasons=["Only officers with grant:manage may modify grant end dates."],
            at=eval_time,
            status_code=403,
        )

    # Dual control if modifying own admin grant
    if grant.user_id == actor.id and is_admin_level_role(db, grant.role_id):
        if not body.approval_id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=grant.id,
                action="grant:manage",
                deny_code="DUAL_CONTROL_APPROVAL_REQUIRED",
                reasons=["Modifying own Admin grant requires second admin approval."],
                at=eval_time,
                status_code=403,
            )
        appr = db.query(Approval).filter(Approval.id == body.approval_id).first()
        if not appr or appr.status != "approved" or appr.used_at is not None or appr.approved_by == actor.id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=grant.id,
                action="grant:manage",
                deny_code="APPROVAL_INVALID",
                reasons=["Valid second admin approval required."],
                at=eval_time,
                status_code=403,
            )
        appr.used_at = to_naive_utc(eval_time)
        appr.status = "used"

    new_vt = to_naive_utc(_parse_at(body.valid_to, "valid_to"))
    grant.valid_to = new_vt

    # F8: Flag in-flight assignments if ended
    if new_vt and new_vt <= to_naive_utc(eval_time) and grant.user:
        flag_user_assignments(db, grant.user, grant, eval_time, f"Grant ended: {body.reason or 'valid_to reached'}")

    # F8: Cascade end-date to child delegations
    cascade_grant_end(db, grant, eval_time, actor.id, new_valid_to=_parse_at(body.valid_to), is_revocation=False)

    record_audit_log(
        session=db,
        at=eval_time,
        event_type="GRANT_CHANGE",
        actor_id=actor.id,
        target_type="grant",
        target_id=grant.id,
        action="GRANT_END_DATE",
        decision="EXECUTED",
        reasons=[body.reason] if body.reason else None,
        details={"valid_to": new_vt.isoformat() if new_vt else None},
    )
    db.commit()
    return _grant_to_response(grant)


@app.delete(
    "/grants/{grant_id}",
    response_model=GrantResponse,
    tags=["Grants"],
    summary="Revoke a grant with mandatory reason",
)
@app.post(
    "/grants/{grant_id}/revoke",
    response_model=GrantResponse,
    tags=["Grants"],
    summary="Revoke a grant with mandatory reason",
)
def revoke_grant(
    grant_id: str = Path(..., description="Grant ID"),
    reason: Optional[str] = Query(None, description="Mandatory reason for revocation"),
    approval_id: Optional[str] = Query(None, description="Dual control approval ID if admin self-grant"),
    at: Optional[str] = Query(None, description="Evaluation timestamp"),
    body: Optional[RevokeGrantRequest] = Body(None),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    actual_reason = (body.reason if body else None) or reason
    actual_approval_id = (body.approval_id if body else None) or approval_id
    actual_at = (body.at if body else None) or at

    if not actual_reason or not actual_reason.strip():
        raise HTTPException(
            status_code=422,
            detail="REASON_REQUIRED: A mandatory written reason is required to revoke a grant.",
        )

    eval_time = _parse_at(actual_at) or datetime.now(timezone.utc)
    grant = db.query(Grant).filter(Grant.id == grant_id).first()
    if not grant:
        raise HTTPException(status_code=404, detail="Grant not found")

    if not has_permission(db, actor, "grant:manage", eval_time):
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="grant",
            target_id=grant.id,
            action="grant:manage",
            deny_code="PERMISSION_DENIED",
            reasons=["Only officers with grant:manage may revoke grants."],
            at=eval_time,
            status_code=403,
        )

    if grant.user_id == actor.id and is_admin_level_role(db, grant.role_id):
        if not actual_approval_id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=grant.id,
                action="grant:manage",
                deny_code="DUAL_CONTROL_APPROVAL_REQUIRED",
                reasons=["Revoking own Admin grant requires second admin approval."],
                at=eval_time,
                status_code=403,
            )
        appr = db.query(Approval).filter(Approval.id == actual_approval_id).first()
        if not appr or appr.status != "approved" or appr.used_at is not None or appr.approved_by == actor.id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="grant",
                target_id=grant.id,
                action="grant:manage",
                deny_code="APPROVAL_INVALID",
                reasons=["Valid second admin approval required."],
                at=eval_time,
                status_code=403,
            )
        appr.used_at = to_naive_utc(eval_time)
        appr.status = "used"

    grant.revoked_at = to_naive_utc(eval_time)
    grant.revocation_reason = actual_reason

    # F8: Flag in-flight assignments
    if grant.user:
        flag_user_assignments(db, grant.user, grant, eval_time, f"Grant revoked: {actual_reason}")

    # F8: Cascade revocation to child delegations
    cascade_grant_end(db, grant, eval_time, actor.id, is_revocation=True, reason=actual_reason)

    record_audit_log(
        session=db,
        at=eval_time,
        event_type="GRANT_CHANGE",
        actor_id=actor.id,
        target_type="grant",
        target_id=grant.id,
        action="GRANT_REVOKE",
        decision="EXECUTED",
        reasons=[actual_reason],
    )
    db.commit()
    return _grant_to_response(grant)


@app.post(
    "/jobs/{job_id}/override",
    tags=["Jobs"],
    summary="Admin override for a job",
)
def override_job(
    job_id: str = Path(..., description="Job ID"),
    body: AdminOverrideRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    if not body.reason or not body.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="REASON_REQUIRED: A mandatory written reason is required for administrative override.",
        )

    # F3: Validate target_stage against allowed stage list (422 otherwise)
    allowed_stages = {"Confirmed", "Prepared", "Checked", "Filed", "Cancelled", "Reopened"}
    if body.target_stage and body.target_stage not in allowed_stages:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid target_stage '{body.target_stage}'. Must be one of: {sorted(allowed_stages)}",
        )

    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        log_access_probe_and_raise_404(actor.id, job_id, "admin:override", eval_time)

    # F4: Permission check admin:override
    if not has_permission(db, actor, "admin:override", eval_time):
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="job",
            target_id=job.id,
            action="admin:override",
            deny_code="PERMISSION_DENIED",
            reasons=["admin:override permission required for administrative override."],
            at=eval_time,
            status_code=403,
        )

    # F3: If job is Filed or locked, or override changes stage away from Filed, or unlocks, require approval_id
    requires_approval = (
        job.stage == "Filed"
        or job.locked
        or (body.target_stage and body.target_stage != "Filed" and job.stage == "Filed")
        or (body.unlock is True and job.locked)
    )

    approved_by_id = None
    if requires_approval:
        if not body.approval_id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="job",
                target_id=job.id,
                action="admin:override",
                deny_code="DUAL_CONTROL_APPROVAL_REQUIRED",
                reasons=["Overriding a filed or locked shipment requires an independent second admin approval."],
                at=eval_time,
                status_code=403,
            )

        appr = db.query(Approval).filter(Approval.id == body.approval_id).first()
        if not appr or appr.status != "approved" or appr.used_at is not None or appr.target_id != job.id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="job",
                target_id=job.id,
                action="admin:override",
                deny_code="APPROVAL_INVALID",
                reasons=["Approval token is invalid, already used, or not targeted to this job."],
                at=eval_time,
                status_code=403,
            )

        if appr.action and appr.action not in ("admin:override", "JOB_OVERRIDE"):
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="job",
                target_id=job.id,
                action="admin:override",
                deny_code="APPROVAL_ACTION_MISMATCH",
                reasons=[f"Approval action '{appr.action}' does not permit admin:override."],
                at=eval_time,
                status_code=403,
            )

        if appr.approved_by == actor.id:
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="job",
                target_id=job.id,
                action="admin:override",
                deny_code="SELF_APPROVAL_PROHIBITED",
                reasons=["Self-approval prohibited: override requires an independent second admin."],
                at=eval_time,
                status_code=403,
            )

        approver_user = db.query(User).filter(User.id == appr.approved_by).first()
        if not approver_user or not has_permission(db, approver_user, "admin:override", eval_time):
            log_action_denial_and_raise(
                actor_id=actor.id,
                target_type="job",
                target_id=job.id,
                action="admin:override",
                deny_code="APPROVER_NOT_AUTHORIZED",
                reasons=["Approver does not hold admin:override authority."],
                at=eval_time,
                status_code=403,
            )

        appr.used_at = to_naive_utc(eval_time)
        appr.status = "used"
        approved_by_id = appr.approved_by

    old_snapshot = {
        "stage": job.stage,
        "locked": job.locked,
        "version": job.version,
    }

    if body.target_stage:
        job.stage = body.target_stage
        job.version += 1
    if body.unlock is not None:
        job.locked = not body.unlock
        job.locked_reason = f"Admin override by {actor.username} (Approved by: {approved_by_id or 'N/A'}): {body.reason}"

    new_snapshot = {
        "stage": job.stage,
        "locked": job.locked,
        "version": job.version,
    }

    # Record both people in job_events and audit_log
    event = JobEvent(
        job_id=job.id,
        actor_id=actor.id,
        action="OVERRIDE",
        from_stage=old_snapshot["stage"],
        to_stage=job.stage,
        reason=f"{body.reason} (Dual control approved by: {approved_by_id})" if approved_by_id else body.reason,
        old_values=json.dumps(old_snapshot),
        new_values=json.dumps(new_snapshot),
        timestamp=to_naive_utc(eval_time),
    )
    db.add(event)

    record_audit_log(
        session=db,
        at=eval_time,
        event_type="OVERRIDE",
        actor_id=actor.id,
        target_type="job",
        target_id=job.id,
        action=body.action,
        decision="EXECUTED",
        reasons=[body.reason],
        details={
            "target_stage": body.target_stage,
            "unlock": body.unlock,
            "approved_by": approved_by_id,
        },
    )
    db.commit()

    return {
        "success": True,
        "job_id": job.id,
        "stage": job.stage,
        "locked": job.locked,
        "version": job.version,
        "actor_id": actor.id,
        "approved_by": approved_by_id,
        "message": f"Job {job.id} overridden by admin {actor.username}.",
    }


@app.post(
    "/jobs/{job_id}/scope",
    tags=["Jobs"],
    summary="Edit scope of a job",
)
@app.patch(
    "/jobs/{job_id}/scope",
    tags=["Jobs"],
    summary="Edit scope of a job",
)
def edit_job_scope(
    job_id: str = Path(..., description="Job ID"),
    body: ScopeEditRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    if not body.reason or not body.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="REASON_REQUIRED: A mandatory written reason is required for scope edit.",
        )

    # F6: expected_version is required (422 when missing)
    if body.expected_version is None:
        raise HTTPException(
            status_code=422,
            detail="expected_version is required on scope edits",
        )

    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)

    # F6: Validate direction and mode
    if body.direction and body.direction not in {"IMPORT", "EXPORT"}:
        raise HTTPException(status_code=422, detail="direction must be 'IMPORT' or 'EXPORT'")
    if body.mode and body.mode not in {"AIR", "FCL", "LCL"}:
        raise HTTPException(status_code=422, detail="mode must be 'AIR', 'FCL', or 'LCL'")
    if body.client_id and not db.query(Client).filter(Client.id == body.client_id).first():
        raise HTTPException(status_code=422, detail=f"Client '{body.client_id}' does not exist.")
    if body.branch_id and not db.query(Branch).filter(Branch.id == body.branch_id).first():
        raise HTTPException(status_code=422, detail=f"Branch '{body.branch_id}' does not exist.")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        log_access_probe_and_raise_404(actor.id, job_id, "job:edit_scope", eval_time)

    current_scope = {
        "client_id": job.client_id,
        "branch_id": job.branch_id,
        "direction": job.direction,
        "mode": job.mode,
    }
    if not has_permission(db, actor, "job:edit_scope", eval_time, scope=current_scope):
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="job",
            target_id=job.id,
            action="job:edit_scope",
            deny_code="PERMISSION_DENIED",
            reasons=["Caller lacks job:edit_scope permission."],
            at=eval_time,
            status_code=403,
        )

    # Filed or locked jobs return FILED_JOB_SCOPE_LOCKED
    if job.stage == "Filed" or job.locked:
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="job",
            target_id=job.id,
            action="job:edit_scope",
            deny_code="FILED_JOB_SCOPE_LOCKED",
            reasons=["Filed or locked shipments cannot have their scope edited directly. Reopen first."],
            at=eval_time,
            status_code=403,
        )

    # F6: Authorization happens BEFORE version check, so unauthorized callers learn nothing
    dec_current = decide(
        user_or_id=actor,
        action="job:edit_scope",
        job_or_id=job,
        at=eval_time,
        session=db,
        log_denials=False,
        approval_id=body.approval_id,
    )
    if not dec_current.allowed:
        if dec_current.deny_code == "NOT_FOUND":
            log_access_probe_and_raise_404(actor.id, job.id, "job:edit_scope", eval_time)
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="job",
            target_id=job.id,
            action="job:edit_scope",
            deny_code=dec_current.deny_code,
            reasons=dec_current.reasons,
            at=eval_time,
            status_code=403,
        )

    # Must be authorized for BOTH current scope and new scope
    new_scope = {
        "client_id": body.client_id or job.client_id,
        "branch_id": body.branch_id or job.branch_id,
        "direction": body.direction or job.direction,
        "mode": body.mode or job.mode,
    }
    if not has_permission(db, actor, "job:edit_scope", eval_time, scope=new_scope):
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="job",
            target_id=job.id,
            action="job:edit_scope",
            deny_code="OUT_OF_SCOPE",
            reasons=[f"Caller lacks job:edit_scope permission for target scope {new_scope}."],
            at=eval_time,
            status_code=403,
        )

    # Now verify expected_version (stale returns 409)
    if job.version != body.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job version mismatch: expected {body.expected_version}, but job is at version {job.version}.",
        )

    # Consume exact approval validated by decide()
    if dec_current.approval_id:
        appr = db.query(Approval).filter(Approval.id == dec_current.approval_id).first()
        if appr:
            appr.used_at = to_naive_utc(eval_time)
            appr.status = "used"

    old_snapshot = {
        "client_id": job.client_id,
        "branch_id": job.branch_id,
        "direction": job.direction,
        "mode": job.mode,
        "version": job.version,
    }

    if body.client_id:
        job.client_id = body.client_id
    if body.branch_id:
        job.branch_id = body.branch_id
    if body.direction:
        job.direction = body.direction
    if body.mode:
        job.mode = body.mode

    job.version += 1

    new_snapshot = {
        "client_id": job.client_id,
        "branch_id": job.branch_id,
        "direction": job.direction,
        "mode": job.mode,
        "version": job.version,
    }

    event = JobEvent(
        job_id=job.id,
        actor_id=actor.id,
        action="EDIT_SCOPE",
        from_stage=job.stage,
        to_stage=job.stage,
        reason=body.reason,
        old_values=json.dumps(old_snapshot),
        new_values=json.dumps(new_snapshot),
        timestamp=to_naive_utc(eval_time),
    )
    db.add(event)

    record_audit_log(
        session=db,
        at=eval_time,
        event_type="JOB_MUTATION",
        actor_id=actor.id,
        target_type="job",
        target_id=job.id,
        action="job:edit_scope",
        decision="EXECUTED",
        reasons=[body.reason],
        details={"old": old_snapshot, "new": new_snapshot},
    )
    db.commit()

    return {
        "success": True,
        "job_id": job.id,
        "version": job.version,
        "client_id": job.client_id,
        "branch_id": job.branch_id,
        "direction": job.direction,
        "mode": job.mode,
    }


@app.post(
    "/users/{user_id}/deactivate",
    tags=["Users"],
    summary="Atomically deactivate a user (leaver process)",
)
def deactivate_user(
    user_id: str = Path(..., description="Target User ID or username to deactivate"),
    body: DeactivateUserRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)
    target_user = _resolve_user(user_id, db)

    # F4: user:manage permission check
    if not has_permission(db, actor, "user:manage", eval_time):
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="user",
            target_id=target_user.id,
            action="user:manage",
            deny_code="PERMISSION_DENIED",
            reasons=["user:manage permission required to deactivate accounts."],
            at=eval_time,
            status_code=403,
        )

    target_user.is_active = False
    target_user.deactivated_at = to_naive_utc(eval_time)

    active_grants = (
        db.query(Grant)
        .filter(Grant.user_id == target_user.id, Grant.revoked_at.is_(None))
        .all()
    )
    for g in active_grants:
        g.revoked_at = to_naive_utc(eval_time)
        g.revocation_reason = f"User deactivated: {body.reason}"

    delegations = (
        db.query(Grant)
        .filter(Grant.granted_by == target_user.id, Grant.source == "delegation", Grant.revoked_at.is_(None))
        .all()
    )
    for d in delegations:
        d.revoked_at = to_naive_utc(eval_time)
        d.revocation_reason = f"Delegator deactivated: {body.reason}"

    # Deactivation keeps flagging everything
    active_assignments = (
        db.query(Assignment)
        .filter(Assignment.user_id == target_user.id, Assignment.is_active == True)
        .all()
    )
    for a in active_assignments:
        a.needs_reassignment = True
        a.reassignment_reason = f"User deactivated / leaver: {body.reason}"

    record_audit_log(
        session=db,
        at=eval_time,
        event_type="USER_DEACTIVATION",
        actor_id=actor.id,
        target_type="user",
        target_id=target_user.id,
        action="USER_DEACTIVATE",
        decision="EXECUTED",
        reasons=[body.reason] if body.reason else None,
        details={
            "grants_revoked": len(active_grants),
            "delegations_revoked": len(delegations),
            "assignments_flagged": len(active_assignments),
        },
    )
    db.commit()

    return {
        "success": True,
        "user_id": target_user.id,
        "username": target_user.username,
        "is_active": False,
        "grants_revoked": len(active_grants),
        "delegations_revoked": len(delegations),
        "assignments_flagged": len(active_assignments),
    }


@app.post(
    "/users/{user_id}/transfer",
    tags=["Users"],
    summary="Transfer a user between branches",
)
def transfer_user(
    user_id: str = Path(..., description="User ID or username to transfer"),
    body: TransferUserRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)
    target_user = _resolve_user(user_id, db)

    # F4: user:manage permission check
    if not has_permission(db, actor, "user:manage", eval_time):
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="user",
            target_id=target_user.id,
            action="user:manage",
            deny_code="PERMISSION_DENIED",
            reasons=["user:manage permission required to transfer users."],
            at=eval_time,
            status_code=403,
        )

    from_b = db.query(Branch).filter(Branch.id == body.from_branch_id).first()
    to_b = db.query(Branch).filter(Branch.id == body.to_branch_id).first()
    if not from_b:
        raise HTTPException(status_code=404, detail=f"Source branch '{body.from_branch_id}' not found.")
    if not to_b:
        raise HTTPException(status_code=404, detail=f"Destination branch '{body.to_branch_id}' not found.")

    old_grants = [
        g for g in db.query(Grant).filter(Grant.user_id == target_user.id, Grant.branch_id == body.from_branch_id).all()
        if g.is_active_at(eval_time)
    ]

    new_grants_created = []
    flagged_count = 0
    for g in old_grants:
        g.revoked_at = to_naive_utc(eval_time)
        g.revocation_reason = f"Transferred to branch {body.to_branch_id}: {body.reason}"

        # F8: Flag assignments only if user has no other active grant covering the job
        flagged_count += flag_user_assignments(
            db, target_user, g, eval_time, f"User transferred from {body.from_branch_id} to {body.to_branch_id}"
        )

        new_g = Grant(
            user_id=target_user.id,
            role_id=g.role_id,
            client_id=g.client_id,
            branch_id=body.to_branch_id,
            direction=g.direction,
            mode=g.mode,
            valid_from=to_naive_utc(eval_time),
            valid_to=g.valid_to,
            source="direct",
            granted_by=actor.id,
        )
        db.add(new_g)
        db.flush()
        new_grants_created.append(new_g.id)

    record_audit_log(
        session=db,
        at=eval_time,
        event_type="BRANCH_TRANSFER",
        actor_id=actor.id,
        target_type="user",
        target_id=target_user.id,
        action="USER_TRANSFER",
        decision="EXECUTED",
        reasons=[body.reason] if body.reason else None,
        details={
            "from_branch": body.from_branch_id,
            "to_branch": body.to_branch_id,
            "old_grants_ended": len(old_grants),
            "new_grants": new_grants_created,
            "assignments_flagged": flagged_count,
        },
    )
    db.commit()

    return {
        "success": True,
        "user_id": target_user.id,
        "from_branch_id": body.from_branch_id,
        "to_branch_id": body.to_branch_id,
        "old_grants_ended": len(old_grants),
        "new_grants_created": new_grants_created,
        "assignments_flagged": flagged_count,
    }


# ---------------------------------------------------------------------------
# Approvals Endpoints (F7)
# ---------------------------------------------------------------------------

@app.post(
    "/approvals",
    response_model=ApprovalResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Approvals"],
    summary="Create a dual-control approval request",
)
def create_approval(
    body: ApprovalRequest = Body(...),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    if not body.reason or not body.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="Mandatory reason required for approval request.",
        )

    eval_time = _parse_at(body.at) or datetime.now(timezone.utc)
    # Default expiry: 24 hours from eval_time
    expires_at = eval_time + timedelta(hours=24)

    appr = Approval(
        action=body.action,
        target_type=body.target_type,
        target_id=body.target_id,
        requested_by=actor.id,
        reason=body.reason,
        status="pending",
        expires_at=to_naive_utc(expires_at),
        created_at=to_naive_utc(eval_time),
        is_active=True,
    )
    db.add(appr)
    db.commit()
    db.refresh(appr)
    return _approval_to_response(appr)


@app.post(
    "/approvals/{approval_id}/approve",
    response_model=ApprovalResponse,
    tags=["Approvals"],
    summary="Approve a dual-control approval request",
)
def approve_request(
    approval_id: str = Path(..., description="Approval ID"),
    at: Optional[str] = Query(None, description="Evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    appr = db.query(Approval).filter(Approval.id == approval_id).first()
    if not appr:
        raise HTTPException(status_code=404, detail="Approval not found")

    if appr.status != "pending":
        raise HTTPException(status_code=422, detail=f"Cannot approve: approval status is '{appr.status}'")

    # Approver must differ from requester
    if appr.requested_by == actor.id:
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="approval",
            target_id=appr.id,
            action="approval:approve",
            deny_code="SELF_APPROVAL_PROHIBITED",
            reasons=["Approver cannot approve their own high-risk operation request."],
            at=eval_time,
            status_code=403,
        )

    # Approver authority check in scope of target
    scope = None
    if appr.target_type == "job":
        job = db.query(Job).filter(Job.id == appr.target_id).first()
        if job:
            scope = {
                "client_id": job.client_id,
                "branch_id": job.branch_id,
                "direction": job.direction,
                "mode": job.mode,
            }

    approver_has_auth = (
        has_permission(db, actor, appr.action, eval_time, scope=scope)
        or has_permission(db, actor, "admin:override", eval_time)
    )
    if not approver_has_auth:
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="approval",
            target_id=appr.id,
            action="approval:approve",
            deny_code="APPROVER_NOT_AUTHORIZED",
            reasons=[f"Approver '{actor.username}' does not hold required permission '{appr.action}' in scope."],
            at=eval_time,
            status_code=403,
        )

    appr.status = "approved"
    appr.approved_by = actor.id
    appr.approved_at = to_naive_utc(eval_time)
    db.commit()
    db.refresh(appr)
    return _approval_to_response(appr)


@app.post(
    "/approvals/{approval_id}/reject",
    response_model=ApprovalResponse,
    tags=["Approvals"],
    summary="Reject a dual-control approval request",
)
def reject_request(
    approval_id: str = Path(..., description="Approval ID"),
    at: Optional[str] = Query(None, description="Evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    appr = db.query(Approval).filter(Approval.id == approval_id).first()
    if not appr:
        raise HTTPException(status_code=404, detail="Approval not found")

    if appr.status != "pending":
        raise HTTPException(status_code=422, detail=f"Cannot reject: approval status is '{appr.status}'")

    if appr.requested_by == actor.id:
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="approval",
            target_id=appr.id,
            action="approval:reject",
            deny_code="SELF_APPROVAL_PROHIBITED",
            reasons=["Approver cannot reject their own request."],
            at=eval_time,
            status_code=403,
        )

    scope = None
    if appr.target_type == "job":
        job = db.query(Job).filter(Job.id == appr.target_id).first()
        if job:
            scope = {
                "client_id": job.client_id,
                "branch_id": job.branch_id,
                "direction": job.direction,
                "mode": job.mode,
            }

    approver_has_auth = (
        has_permission(db, actor, appr.action, eval_time, scope=scope)
        or has_permission(db, actor, "admin:override", eval_time)
    )
    if not approver_has_auth:
        log_action_denial_and_raise(
            actor_id=actor.id,
            target_type="approval",
            target_id=appr.id,
            action="approval:reject",
            deny_code="APPROVER_NOT_AUTHORIZED",
            reasons=[f"Approver '{actor.username}' does not hold required permission '{appr.action}' in scope."],
            at=eval_time,
            status_code=403,
        )

    appr.status = "rejected"
    appr.approved_by = actor.id
    appr.approved_at = to_naive_utc(eval_time)
    db.commit()
    db.refresh(appr)
    return _approval_to_response(appr)


@app.get(
    "/approvals/{approval_id}",
    response_model=ApprovalResponse,
    tags=["Approvals"],
    summary="View an approval request",
)
def get_approval(
    approval_id: str = Path(..., description="Approval ID"),
    at: Optional[str] = Query(None, description="Evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    appr = db.query(Approval).filter(Approval.id == approval_id).first()
    if not appr:
        raise HTTPException(status_code=404, detail="Approval not found")

    # Visible to requester, approvers with permission, and audit:read holders
    is_requester = (appr.requested_by == actor.id)
    has_audit = has_permission(db, actor, "audit:read", eval_time)

    scope = None
    if appr.target_type == "job":
        job = db.query(Job).filter(Job.id == appr.target_id).first()
        if job:
            scope = {
                "client_id": job.client_id,
                "branch_id": job.branch_id,
                "direction": job.direction,
                "mode": job.mode,
            }
    has_approver_perm = has_permission(db, actor, appr.action, eval_time, scope=scope)

    if not (is_requester or has_audit or has_approver_perm):
        raise HTTPException(status_code=404, detail="Approval not found")

    return _approval_to_response(appr)


# ===========================================================================
# Phase 4: Audit, History and Test Hardening Endpoints
# ===========================================================================

class JobEventResponse(BaseModel):
    id: str
    job_id: str
    action: str
    from_stage: Optional[str] = None
    to_stage: str
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    reason: Optional[str] = None
    old_values: Optional[dict] = None
    new_values: Optional[dict] = None
    timestamp: str


class JobHistoryResponse(BaseModel):
    job_id: str
    total_events: int
    events: List[JobEventResponse]


class CandidateUser(BaseModel):
    user_id: str
    username: str
    full_name: str
    role_id: str
    grant_id: str
    grant_source: str
    valid_from: str
    valid_to: Optional[str] = None
    actions: List[str]


class WhoCouldResponse(BaseModel):
    job_id: str
    on_date: str
    candidates: List[CandidateUser]


class AuditLogItemResponse(BaseModel):
    id: int
    timestamp: str
    event_type: str
    actor_id: Optional[str] = None
    target_type: str
    target_id: Optional[str] = None
    action: str
    decision: str
    deny_code: Optional[str] = None
    reasons: Optional[List[str]] = None
    grant_ids_used: Optional[List[str]] = None
    permission_snapshot: Optional[dict] = None
    details: Optional[dict] = None
    prev_hash: str
    record_hash: str


class AuditListResponse(BaseModel):
    total: int
    items: List[AuditLogItemResponse]


class AuditVerifyResponse(BaseModel):
    verified: bool
    total_records: int
    head_hash: Optional[str] = None
    broken_at_id: Optional[int] = None
    expected_hash: Optional[str] = None
    recorded_hash: Optional[str] = None
    error: Optional[str] = None


class DenialAlertItem(BaseModel):
    job_id: str
    actor_id: Optional[str] = None
    actor_name: Optional[str] = None
    action: str
    denial_count: int
    deny_codes: List[str]
    reasons: List[str]
    last_attempt_at: str
    is_filed_job: bool
    risk_level: str


class FiledJobDenialsReport(BaseModel):
    total_filed_job_denials: int
    repeated_denial_incidents: int
    generated_at: str
    incidents: List[DenialAlertItem]


@app.get(
    "/jobs/{job_id}/history",
    response_model=JobHistoryResponse,
    tags=["Jobs"],
    summary="Full timeline of job events, with stage changes, actors, and reasons",
)
def get_job_history(
    job_id: str = Path(..., description="Job ID"),
    at: Optional[str] = Query(None, description="ISO-8601 evaluation timestamp"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        log_access_probe_and_raise_404(actor.id, job_id, "job:view", eval_time)

    # Check caller has permission to view the job
    decision = decide(
        user_or_id=actor,
        action="job:view",
        job_or_id=job,
        at=eval_time,
        session=db,
        log_denials=False,
    )
    if not decision.allowed:
        log_access_probe_and_raise_404(actor.id, job.id, "job:view", eval_time)

    job_scope = {
        "client_id": job.client_id,
        "branch_id": job.branch_id,
        "direction": job.direction,
        "mode": job.mode,
    }
    has_internal = has_permission(db, actor, "job:view_internal", eval_time, scope=job_scope)
    is_client_user = (actor.user_type in ("client", "client_user")) or not has_internal

    events = (
        db.query(JobEvent)
        .filter(JobEvent.job_id == job_id)
        .order_by(JobEvent.timestamp.asc(), JobEvent.id.asc())
        .all()
    )

    result_events = []
    for ev in events:
        if is_client_user:
            # Client users see stage changes only. Internal notes, reasons, and staff names are excluded
            result_events.append(
                JobEventResponse(
                    id=ev.id,
                    job_id=ev.job_id,
                    action=ev.action,
                    from_stage=ev.from_stage,
                    to_stage=ev.to_stage,
                    actor_id=None,
                    actor_name=None,
                    reason=None,
                    old_values=None,
                    new_values=None,
                    timestamp=ev.timestamp.isoformat() if ev.timestamp else "",
                )
            )
        else:
            old_val = json.loads(ev.old_values) if ev.old_values else None
            new_val = json.loads(ev.new_values) if ev.new_values else None
            actor_name = None
            if ev.actor_id:
                u = db.query(User).filter(User.id == ev.actor_id).first()
                actor_name = u.username if u else ev.actor_id
            result_events.append(
                JobEventResponse(
                    id=ev.id,
                    job_id=ev.job_id,
                    action=ev.action,
                    from_stage=ev.from_stage,
                    to_stage=ev.to_stage,
                    actor_id=ev.actor_id,
                    actor_name=actor_name,
                    reason=ev.reason,
                    old_values=old_val,
                    new_values=new_val,
                    timestamp=ev.timestamp.isoformat() if ev.timestamp else "",
                )
            )

    return JobHistoryResponse(
        job_id=job.id,
        total_events=len(result_events),
        events=result_events,
    )


@app.get(
    "/jobs/{job_id}/who-could",
    response_model=WhoCouldResponse,
    tags=["Jobs"],
    summary="Replay end-dated grants to list everyone who could have changed the job on a date",
)
def get_who_could_change_job(
    job_id: str = Path(..., description="Job ID"),
    on: Optional[str] = Query(None, description="Replay date (ISO-8601 or YYYY-MM-DD)"),
    at: Optional[str] = Query(None, description="Alias for 'on'"),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    target_date_str = on or at
    eval_time = _parse_at(target_date_str) or datetime.now(timezone.utc)
    eval_time_naive = to_naive_utc(eval_time)

    # Client users cannot access audit replay
    if actor.user_type in ("client", "client_user"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client users are not permitted to access who-could audit history",
        )

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        log_access_probe_and_raise_404(actor.id, job_id, "job:view", eval_time)

    # Out-of-scope check
    decision = decide(
        user_or_id=actor,
        action="job:view",
        job_or_id=job,
        at=eval_time,
        session=db,
        log_denials=False,
    )
    if not decision.allowed:
        log_access_probe_and_raise_404(actor.id, job.id, "job:view", eval_time)

    # Active grants on target date
    grants = (
        db.query(Grant)
        .filter(
            Grant.valid_from <= eval_time_naive,
            (Grant.valid_to == None) | (Grant.valid_to >= eval_time_naive),
            (Grant.revoked_at == None) | (Grant.revoked_at >= eval_time_naive),
        )
        .all()
    )

    CHANGE_ACTIONS = {
        "job:prepare",
        "job:check",
        "job:file",
        "job:cancel_filed",
        "job:reopen_closed",
        "job:edit_scope",
        "admin:override",
    }

    candidates = []
    seen = set()

    for g in grants:
        # Check scope containment on job
        if g.client_id is not None and g.client_id != job.client_id:
            continue
        if g.branch_id is not None and g.branch_id != job.branch_id:
            continue
        if g.direction is not None and g.direction != job.direction:
            continue
        if g.mode is not None and g.mode != job.mode:
            continue

        user = db.query(User).filter(User.id == g.user_id).first()
        if not user:
            continue
        if user.deactivated_at and to_naive_utc(user.deactivated_at) <= eval_time_naive:
            continue

        if g.source == "delegation":
            if not g.parent_grant_id:
                continue
            parent = db.query(Grant).filter(Grant.id == g.parent_grant_id).first()
            if not parent:
                continue
            if parent.valid_from and parent.valid_from > eval_time_naive:
                continue
            if parent.valid_to and parent.valid_to < eval_time_naive:
                continue
            if parent.revoked_at and parent.revoked_at < eval_time_naive:
                continue

        role_perms = (
            db.query(RolePermission)
            .filter(RolePermission.role_id == g.role_id)
            .all()
        )
        actions = sorted([rp.permission_id for rp in role_perms if rp.permission_id in CHANGE_ACTIONS])
        if not actions:
            continue

        key = (user.id, g.role_id, g.id)
        if key not in seen:
            seen.add(key)
            candidates.append(
                CandidateUser(
                    user_id=user.id,
                    username=user.username,
                    full_name=user.full_name,
                    role_id=g.role_id,
                    grant_id=g.id,
                    grant_source=g.source,
                    valid_from=g.valid_from.isoformat() if g.valid_from else "",
                    valid_to=g.valid_to.isoformat() if g.valid_to else None,
                    actions=actions,
                )
            )

    return WhoCouldResponse(
        job_id=job.id,
        on_date=eval_time.isoformat(),
        candidates=candidates,
    )


@app.get(
    "/audit",
    response_model=AuditListResponse,
    tags=["Audit"],
    summary="List append-only audit trail logs",
)
def list_audit_logs(
    event_type: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    target_id: Optional[str] = Query(None),
    actor_id: Optional[str] = Query(None),
    decision: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    at: Optional[str] = Query(None),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    if not has_permission(db, actor, "audit:read", eval_time):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PERMISSION_DENIED: audit:read permission is required to view audit logs",
        )

    q = db.query(AuditLog)
    if event_type:
        q = q.filter(AuditLog.event_type == event_type)
    if target_type:
        q = q.filter(AuditLog.target_type == target_type)
    if target_id:
        q = q.filter(AuditLog.target_id == target_id)
    if actor_id:
        q = q.filter(AuditLog.actor_id == actor_id)
    if decision:
        q = q.filter(AuditLog.decision == decision)

    total = q.count()
    items = q.order_by(AuditLog.id.asc()).offset(offset).limit(limit).all()

    resp_items = []
    for it in items:
        reasons_list = None
        if it.reasons:
            try:
                reasons_list = json.loads(it.reasons)
                if not isinstance(reasons_list, list):
                    reasons_list = [str(reasons_list)]
            except Exception:
                reasons_list = [it.reasons]

        g_ids = None
        if it.grant_ids_used:
            try:
                g_ids = json.loads(it.grant_ids_used)
            except Exception:
                g_ids = [it.grant_ids_used]

        snap = None
        if it.permission_snapshot:
            try:
                snap = json.loads(it.permission_snapshot)
            except Exception:
                snap = {"raw": it.permission_snapshot}

        det = None
        if it.details:
            try:
                det = json.loads(it.details)
            except Exception:
                det = {"raw": it.details}

        resp_items.append(
            AuditLogItemResponse(
                id=it.id,
                timestamp=it.timestamp.isoformat() if it.timestamp else "",
                event_type=it.event_type,
                actor_id=it.actor_id,
                target_type=it.target_type,
                target_id=it.target_id,
                action=it.action,
                decision=it.decision,
                deny_code=it.deny_code,
                reasons=reasons_list,
                grant_ids_used=g_ids,
                permission_snapshot=snap,
                details=det,
                prev_hash=it.prev_hash,
                record_hash=it.record_hash,
            )
        )

    return AuditListResponse(total=total, items=resp_items)


@app.get(
    "/audit/verify",
    response_model=AuditVerifyResponse,
    tags=["Audit"],
    summary="Recompute and verify the cryptographic hash chain of the audit log",
)
def verify_audit_chain(
    at: Optional[str] = Query(None),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    if not (has_permission(db, actor, "audit:verify", eval_time) or has_permission(db, actor, "audit:read", eval_time)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PERMISSION_DENIED: audit:verify permission is required to verify the audit chain",
        )

    records = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
    if not records:
        return AuditVerifyResponse(verified=True, total_records=0)

    prev_expected_hash = "0" * 64
    for idx, rec in enumerate(records):
        # 1. Check prev_hash links to prior record's record_hash
        if idx == 0:
            if rec.prev_hash != "0" * 64:
                return AuditVerifyResponse(
                    verified=False,
                    total_records=len(records),
                    broken_at_id=rec.id,
                    expected_hash="0" * 64,
                    recorded_hash=rec.prev_hash,
                    error=f"Genesis record #{rec.id} prev_hash broken: expected 0*64, got {rec.prev_hash}",
                )
        else:
            if rec.prev_hash != prev_expected_hash:
                return AuditVerifyResponse(
                    verified=False,
                    total_records=len(records),
                    broken_at_id=rec.id,
                    expected_hash=prev_expected_hash,
                    recorded_hash=rec.prev_hash,
                    error=f"Chain broken at record #{rec.id}: prev_hash does not match previous record_hash",
                )

        # 2. Check record payload integrity
        if not rec.verify_integrity():
            expected = AuditLog.compute_hash(
                prev_hash=rec.prev_hash,
                timestamp=rec.timestamp,
                event_type=rec.event_type,
                actor_id=rec.actor_id,
                target_type=rec.target_type,
                target_id=rec.target_id,
                action=rec.action,
                decision=rec.decision,
                deny_code=rec.deny_code,
                reasons=rec.reasons,
                grant_ids_used=rec.grant_ids_used,
                permission_snapshot=rec.permission_snapshot,
                details=rec.details,
            )
            return AuditVerifyResponse(
                verified=False,
                total_records=len(records),
                broken_at_id=rec.id,
                expected_hash=expected,
                recorded_hash=rec.record_hash,
                error=f"Data tampering detected at record #{rec.id}: payload does not hash to record_hash",
            )

        prev_expected_hash = rec.record_hash

    return AuditVerifyResponse(
        verified=True,
        total_records=len(records),
        head_hash=records[-1].record_hash,
    )


@app.get(
    "/audit/reports/filed-job-denials",
    response_model=FiledJobDenialsReport,
    tags=["Audit"],
    summary="Report repeated denials against filed jobs (early warning incident detection)",
)
def report_filed_job_denials(
    at: Optional[str] = Query(None),
    actor: User = Depends(get_current_actor),
    db: Session = Depends(get_db),
):
    eval_time = _parse_at(at) or datetime.now(timezone.utc)
    if not has_permission(db, actor, "audit:read", eval_time):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PERMISSION_DENIED: audit:read permission is required to view incident reports",
        )

    # Denials against jobs
    denials = (
        db.query(AuditLog)
        .filter(AuditLog.decision == "DENY", AuditLog.target_type == "job")
        .order_by(AuditLog.timestamp.asc())
        .all()
    )

    grouped = {}
    for log in denials:
        job = db.query(Job).filter(Job.id == log.target_id).first() if log.target_id else None
        is_filed = (job and job.stage == "Filed") or (log.action in ("job:cancel_filed", "job:reopen_closed", "job:file"))
        key = (log.target_id, log.actor_id, log.action)
        if key not in grouped:
            grouped[key] = {
                "job_id": log.target_id or "UNKNOWN",
                "actor_id": log.actor_id,
                "action": log.action,
                "count": 0,
                "deny_codes": set(),
                "reasons": set(),
                "latest_ts": log.timestamp,
                "is_filed": is_filed,
            }
        grouped[key]["count"] += 1
        if log.deny_code:
            grouped[key]["deny_codes"].add(log.deny_code)
        if log.reasons:
            try:
                r_parsed = json.loads(log.reasons)
                if isinstance(r_parsed, list):
                    grouped[key]["reasons"].update(r_parsed)
                else:
                    grouped[key]["reasons"].add(str(r_parsed))
            except Exception:
                grouped[key]["reasons"].add(log.reasons)
        if log.timestamp and (grouped[key]["latest_ts"] is None or log.timestamp > grouped[key]["latest_ts"]):
            grouped[key]["latest_ts"] = log.timestamp

    incidents = []
    total_filed_denials = 0
    repeated_incidents = 0

    for item in grouped.values():
        if item["is_filed"]:
            total_filed_denials += item["count"]
        if item["count"] >= 2 or item["is_filed"]:
            repeated_incidents += 1
            u = db.query(User).filter(User.id == item["actor_id"]).first() if item["actor_id"] else None
            risk = "CRITICAL" if (item["is_filed"] and item["count"] >= 2) else ("HIGH" if item["is_filed"] else "MEDIUM")
            incidents.append(
                DenialAlertItem(
                    job_id=item["job_id"],
                    actor_id=item["actor_id"],
                    actor_name=u.username if u else item["actor_id"],
                    action=item["action"],
                    denial_count=item["count"],
                    deny_codes=sorted(list(item["deny_codes"])),
                    reasons=sorted(list(item["reasons"])),
                    last_attempt_at=item["latest_ts"].isoformat() if item["latest_ts"] else "",
                    is_filed_job=item["is_filed"],
                    risk_level=risk,
                )
            )

    return FiledJobDenialsReport(
        total_filed_job_denials=total_filed_denials,
        repeated_denial_incidents=repeated_incidents,
        generated_at=eval_time.isoformat(),
        incidents=incidents,
    )


# ---------------------------------------------------------------------------
# Root Redirect to OpenAPI Documentation
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse(url="/docs")


