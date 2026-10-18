import json
import logging
from datetime import datetime, timezone
from typing import Optional, Union
from sqlalchemy.orm import Session

from src.models import (
    User,
    Job,
    Grant,
    Role,
    Permission,
    RolePermission,
    Approval,
    AuditLog,
)
from src.engine.types import DecisionResult

logger = logging.getLogger("access_control.engine")


def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if hasattr(dt, "tzinfo") and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


STAGE_ALLOWED_ACTIONS = {
    "Confirmed": {"job:view", "job:view_internal", "job:create", "job:prepare", "job:edit_scope", "job:assign"},
    "Prepared": {"job:view", "job:view_internal", "job:check", "job:edit_scope", "job:assign"},
    "Checked": {"job:view", "job:view_internal", "job:file", "job:edit_scope", "job:assign"},
    "Filed": {"job:view", "job:cancel_filed", "job:reopen_closed"},
    "Cancelled": {"job:view", "job:reopen_closed"},
    "Reopened": {"job:view", "job:view_internal", "job:prepare", "job:check", "job:file", "job:edit_scope", "job:assign"},
}

HIGH_RISK_ACTION_MAPPING = {
    "job:cancel_filed": "JOB_CANCEL_FILED",
    "job:reopen_closed": "JOB_REOPEN",
    "job:edit_scope": "JOB_SCOPE_EDIT",
}


def log_denial_to_audit(
    session: Session,
    user_id: Optional[str],
    job_id: Optional[str],
    action: str,
    deny_code: str,
    reasons: list[str],
    at: datetime,
) -> None:
    """Appends an unalterable, hash-chained denial entry to audit_log."""
    try:
        last_log = session.query(AuditLog).order_by(AuditLog.id.desc()).first()
        prev_h = last_log.record_hash if last_log else "0" * 64

        rec_hash = AuditLog.compute_hash(
            prev_hash=prev_h,
            timestamp=at,
            event_type="DENIAL",
            actor_id=user_id,
            target_type="job",
            target_id=job_id,
            action=action,
            decision="DENY",
            deny_code=deny_code,
            reasons=json.dumps(reasons),
            grant_ids_used=None,
            permission_snapshot=None,
            details=None,
        )

        entry = AuditLog(
            timestamp=at,
            event_type="DENIAL",
            actor_id=user_id,
            target_type="job",
            target_id=job_id,
            action=action,
            decision="DENY",
            deny_code=deny_code,
            reasons=json.dumps(reasons),
            prev_hash=prev_h,
            record_hash=rec_hash,
        )
        session.add(entry)
        session.flush()
    except Exception as e:
        logger.error(f"Failed to write denial to audit log: {e}")


def has_permission(
    session: Session,
    user: Union[User, str],
    permission: str,
    at: Optional[datetime] = None,
    scope: Optional[dict] = None,
) -> bool:
    """
    Evaluates whether `user` holds `permission` at evaluation timestamp `at`,
    optionally bounded by `scope` (dict with client_id, branch_id, direction, mode).
    Fails closed: returns False on any ambiguity, inactivity, or missing grant.
    """
    if user is None:
        return False
    if isinstance(user, str):
        user_obj = session.query(User).filter((User.id == user) | (User.username == user)).first()
        if not user_obj:
            return False
        user = user_obj

    if not user.is_active:
        return False

    if at is None:
        at = datetime.now(timezone.utc)

    user_grants = session.query(Grant).filter(Grant.user_id == user.id).all()
    matching_grants = []
    for g in user_grants:
        if not g.is_active_at(at):
            continue
        # Delegation cascade: inactive or revoked parent makes delegation inactive
        if g.source == "delegation" and g.parent_grant_id:
            parent = session.query(Grant).filter(Grant.id == g.parent_grant_id).first()
            if not parent or not parent.is_active_at(at):
                continue
        if scope:
            if g.client_id is not None and scope.get("client_id") != g.client_id:
                continue
            if g.branch_id is not None and scope.get("branch_id") != g.branch_id:
                continue
            if g.direction is not None and scope.get("direction") != g.direction:
                continue
            if g.mode is not None and scope.get("mode") != g.mode:
                continue
        matching_grants.append(g)

    if not matching_grants:
        return False

    role_ids = {g.role_id for g in matching_grants}
    has_perm = (
        session.query(RolePermission)
        .filter(RolePermission.role_id.in_(role_ids), RolePermission.permission_id == permission)
        .first()
    )
    return has_perm is not None


def decide(
    user_or_id: Union[User, str],
    action: str,
    job_or_id: Union[Job, str],
    at: Optional[datetime] = None,
    session: Optional[Session] = None,
    requester_or_id: Optional[Union[User, str]] = None,
    log_denials: bool = True,
    approval_id: Optional[str] = None,
) -> DecisionResult:
    """
    Core Decision Function: decides whether a given person may perform a given action
    on a given job at a given point in time, and explains the answer.

    Evaluation Order:
    1. Existence & Client User Boundary (Out-of-scope returns NOT_FOUND so existence is not confirmed)
    2. User Active Check (Deactivated users denied immediately)
    3. Grant Scope Matching (Scoped RBAC: client, branch, direction, mode)
    4. Role Carries Permission (view vs act separation)
    5. Stage Allows Action (workflow state machine rules, parser limits)
    6. Separation of Duties (Preparer cannot Check own job)
    7. High-Risk Action Dual Control (Cancel filed, Reopen closed, Scope edit need second approver)
    8. Fail-Closed wrapper
    9. Explanation tier tailoring (Admin/Auditor vs Generic)
    """
    if at is None:
        at = datetime.now(timezone.utc)
    at_n = to_naive_utc(at)

    is_admin_or_auditor = False
    tier = "generic"

    try:
        # Resolve objects if IDs or usernames were provided
        user = user_or_id
        if isinstance(user_or_id, str):
            if session is None:
                raise ValueError("Database session required when passing string IDs to decide()")
            user = (
                session.query(User)
                .filter((User.id == user_or_id) | (User.username == user_or_id))
                .first()
            )

        job = job_or_id
        if isinstance(job_or_id, str):
            if session is None:
                raise ValueError("Database session required when passing string IDs to decide()")
            job = session.query(Job).filter(Job.id == job_or_id).first()

        requester = requester_or_id
        if isinstance(requester_or_id, str):
            if session is not None:
                requester = (
                    session.query(User)
                    .filter((User.id == requester_or_id) | (User.username == requester_or_id))
                    .first()
                )
        if requester is None:
            requester = user

        # Determine explanation tier based purely on caller's audit:read permission
        if requester and session:
            if has_permission(session, requester, "audit:read", at):
                is_admin_or_auditor = True

        tier = "full" if is_admin_or_auditor else "generic"

        # -------------------------------------------------------------
        # STEP 1: Existence & Client User Boundary (Q7, Q19)
        # -------------------------------------------------------------
        if job is None:
            return DecisionResult(
                allowed=False,
                deny_code="NOT_FOUND",
                reasons=["Job not found."],
                rule="EXISTENCE_CHECK",
                tier=tier,
            )

        if user is None:
            return DecisionResult(
                allowed=False,
                deny_code="USER_NOT_FOUND",
                reasons=["User not found."],
                rule="EXISTENCE_CHECK",
                tier=tier,
            )

        # Client user boundary: A client user can only see jobs belonging to their client.
        # Any attempt to access a job belonging to another client returns NOT_FOUND (404),
        # never 403, preventing existence confirmation (Q7, Q19).
        if user.user_type == "client_user" and user.client_id != job.client_id:
            return DecisionResult(
                allowed=False,
                deny_code="NOT_FOUND",
                reasons=["Job not found."],
                rule="CLIENT_ISOLATION_BOUNDARY",
                tier=tier,
            )

        # -------------------------------------------------------------
        # STEP 2: User Status Active Check (Q16)
        # -------------------------------------------------------------
        if not user.is_active:
            res = DecisionResult(
                allowed=False,
                deny_code="USER_INACTIVE",
                reasons=[
                    f"User account '{user.username}' is deactivated or inactive."
                    if tier == "full"
                    else "User account is not active."
                ],
                rule="USER_STATUS_ACTIVE",
                tier=tier,
            )
            if log_denials and session:
                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
            return res

        # -------------------------------------------------------------
        # STEP 3: Grant Scope Matching (Scoped RBAC - Q2)
        # -------------------------------------------------------------
        user_grants = session.query(Grant).filter(Grant.user_id == user.id).all()
        active_grants = []
        for g in user_grants:
            if not g.is_active_at(at):
                continue
            if g.source == "delegation" and g.parent_grant_id:
                parent = session.query(Grant).filter(Grant.id == g.parent_grant_id).first()
                if not parent or not parent.is_active_at(at):
                    continue
            active_grants.append(g)

        # Filter grants whose attribute constraints match the job scope
        matching_scope_grants = []
        for g in active_grants:
            client_match = (g.client_id is None) or (g.client_id == job.client_id)
            branch_match = (g.branch_id is None) or (g.branch_id == job.branch_id)
            direction_match = (g.direction is None) or (g.direction == job.direction)
            mode_match = (g.mode is None) or (g.mode == job.mode)

            if client_match and branch_match and direction_match and mode_match:
                matching_scope_grants.append(g)

        if not matching_scope_grants:
            # Construct clear diagnostic explaining the mismatch
            if tier == "full":
                reasons = [
                    f"User '{user.username}' has no active grant covering job scope "
                    f"[Client: {job.client_id}, Branch: {job.branch_id}, Direction: {job.direction}, Mode: {job.mode}]."
                ]
            else:
                reasons = ["Action not permitted: job scope is outside user's assigned jurisdiction."]

            res = DecisionResult(
                allowed=False,
                deny_code="OUT_OF_SCOPE",
                reasons=reasons,
                grant_ids=[g.id for g in active_grants],
                rule="GRANT_SCOPE_MATCH",
                tier=tier,
            )
            if log_denials and session:
                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
            return res

        # -------------------------------------------------------------
        # STEP 4: Role Carries Permission (View vs Act Separation - Q1, Q12)
        # -------------------------------------------------------------
        # Check permissions attached to the in-scope roles
        in_scope_role_ids = {g.role_id for g in matching_scope_grants}
        rps = (
            session.query(RolePermission)
            .filter(RolePermission.role_id.in_(in_scope_role_ids))
            .all()
        )
        granted_permissions = {rp.permission_id for rp in rps}

        # Client user strict restriction: client users only possess job:view
        if user.user_type == "client_user" and action != "job:view":
            res = DecisionResult(
                allowed=False,
                deny_code="CLIENT_USER_READ_ONLY",
                reasons=["Client users have read-only access to their own shipments."],
                grant_ids=[g.id for g in matching_scope_grants],
                rule="CLIENT_READ_ONLY_GUARDRAIL",
                tier=tier,
            )
            if log_denials and session:
                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
            return res

        if action not in granted_permissions:
            if tier == "full":
                reasons = [
                    f"Roles held in scope {sorted(list(in_scope_role_ids))} do not grant permission '{action}'."
                ]
            else:
                reasons = ["User role does not permit this action."]

            res = DecisionResult(
                allowed=False,
                deny_code="NO_PERMISSION",
                reasons=reasons,
                grant_ids=[g.id for g in matching_scope_grants],
                rule="ROLE_PERMISSION_CHECK",
                tier=tier,
            )
            if log_denials and session:
                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
            return res

        # Grants that specifically contributed to this permission
        contributing_grants = []
        for g in matching_scope_grants:
            g_perms = {rp.permission_id for rp in g.role.permissions}
            if action in g_perms:
                contributing_grants.append(g.id)

        # -------------------------------------------------------------
        # STEP 5: Stage Allows Action (Workflow Guardrails - Q9)
        # -------------------------------------------------------------
        allowed_actions_at_stage = STAGE_ALLOWED_ACTIONS.get(job.stage, set())

        # Special constraint for Parser service account (Q9):
        # Parser can only create/update at Confirmed/Prepared, never touch Filed/Cancelled/Reopened or locked jobs
        if "Parser" in in_scope_role_ids or user.user_type == "service_account":
            if job.stage not in {"Confirmed", "Prepared"} or job.locked:
                res = DecisionResult(
                    allowed=False,
                    deny_code="PARSER_STAGE_RESTRICTION",
                    reasons=["Automated parser service accounts are restricted to Confirmed and Prepared stages on unlocked jobs."],
                    grant_ids=contributing_grants,
                    rule="SERVICE_ACCOUNT_STAGE_RESTRICTION",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

        if action not in allowed_actions_at_stage:
            if tier == "full":
                reasons = [
                    f"Action '{action}' is not permitted while job '{job.id}' is in stage '{job.stage}'."
                ]
            else:
                reasons = [f"This action cannot be performed on a shipment in '{job.stage}' status."]

            res = DecisionResult(
                allowed=False,
                deny_code="STAGE_NOT_PERMITTED",
                reasons=reasons,
                grant_ids=contributing_grants,
                rule="STAGE_WORKFLOW_GUARDRAIL",
                tier=tier,
            )
            if log_denials and session:
                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
            return res

        # -------------------------------------------------------------
        # STEP 6: Separation of Duties (Maker-Checker Conflict - Q3)
        # -------------------------------------------------------------
        if action == "job:check":
            if job.prepared_by and job.prepared_by == user.id:
                res = DecisionResult(
                    allowed=False,
                    deny_code="MAKER_CHECKER_CONFLICT",
                    reasons=[
                        "Separation of duties violation: user prepared this shipment checklist and cannot check or approve their own work."
                    ],
                    grant_ids=contributing_grants,
                    rule="MAKER_CHECKER_SEPARATION_OF_DUTIES",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

        # -------------------------------------------------------------
        # STEP 7: High-Risk Actions & Dual Control (Q6, Q8, F7)
        # -------------------------------------------------------------
        consumed_approval_id = None
        if action in HIGH_RISK_ACTION_MAPPING:
            target_type = HIGH_RISK_ACTION_MAPPING[action]
            approval = None
            if approval_id:
                approval = session.query(Approval).filter(Approval.id == approval_id).first()
                if not approval:
                    res = DecisionResult(
                        allowed=False,
                        deny_code="APPROVAL_NOT_FOUND",
                        reasons=[f"Approval with ID '{approval_id}' was not found."],
                        grant_ids=contributing_grants,
                        rule="DUAL_CONTROL_APPROVAL_REQUIRED",
                        tier=tier,
                    )
                    if log_denials and session:
                        log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                    return res
                if approval.target_id != job.id:
                    res = DecisionResult(
                        allowed=False,
                        deny_code="APPROVAL_TARGET_MISMATCH",
                        reasons=[f"Approval target '{approval.target_id}' does not match job '{job.id}'."],
                        grant_ids=contributing_grants,
                        rule="DUAL_CONTROL_TARGET_MATCH",
                        tier=tier,
                    )
                    if log_denials and session:
                        log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                    return res
                if approval.action and approval.action != action and approval.target_type != target_type:
                    res = DecisionResult(
                        allowed=False,
                        deny_code="APPROVAL_ACTION_MISMATCH",
                        reasons=[f"Approval action '{approval.action}' does not match requested action '{action}'."],
                        grant_ids=contributing_grants,
                        rule="DUAL_CONTROL_ACTION_MATCH",
                        tier=tier,
                    )
                    if log_denials and session:
                        log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                    return res
            else:
                approval = (
                    session.query(Approval)
                    .filter(
                        Approval.target_id == job.id,
                        (Approval.action == action) | (Approval.target_type == target_type),
                        Approval.requested_by == user.id,
                        Approval.is_active == True,
                        (Approval.status == "approved") | ((Approval.status == "pending") & Approval.approved_by.isnot(None)),
                        Approval.used_at.is_(None),
                    )
                    .order_by(Approval.created_at.desc())
                    .first()
                )
                if not approval:
                    candidate = (
                        session.query(Approval)
                        .filter(
                            Approval.target_id == job.id,
                            (Approval.action == action) | (Approval.target_type == target_type),
                            Approval.is_active == True,
                        )
                        .order_by(Approval.created_at.desc())
                        .first()
                    )
                    if candidate:
                        if candidate.requested_by != user.id:
                            res = DecisionResult(
                                allowed=False,
                                deny_code="APPROVAL_REQUESTER_MISMATCH",
                                reasons=["Approval was requested by another user and cannot be claimed."],
                                grant_ids=contributing_grants,
                                rule="DUAL_CONTROL_REQUESTER_BINDING",
                                tier=tier,
                            )
                            if log_denials and session:
                                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                            return res
                        if candidate.approved_by == user.id:
                            res = DecisionResult(
                                allowed=False,
                                deny_code="SELF_APPROVAL_PROHIBITED",
                                reasons=["User cannot approve their own high-risk operation request."],
                                grant_ids=contributing_grants,
                                rule="DUAL_CONTROL_SELF_APPROVAL",
                                tier=tier,
                            )
                            if log_denials and session:
                                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                            return res
                        if candidate.used_at is not None or candidate.status == "used":
                            res = DecisionResult(
                                allowed=False,
                                deny_code="APPROVAL_ALREADY_USED",
                                reasons=["Approval token has already been executed (single-use limit)."],
                                grant_ids=contributing_grants,
                                rule="DUAL_CONTROL_SINGLE_USE",
                                tier=tier,
                            )
                            if log_denials and session:
                                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                            return res
                        exp_n = to_naive_utc(candidate.expires_at)
                        if exp_n and at_n > exp_n:
                            res = DecisionResult(
                                allowed=False,
                                deny_code="APPROVAL_EXPIRED",
                                reasons=["High-risk action approval has expired."],
                                grant_ids=contributing_grants,
                                rule="DUAL_CONTROL_TIME_WINDOW",
                                tier=tier,
                            )
                            if log_denials and session:
                                log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                            return res

                    res = DecisionResult(
                        allowed=False,
                        deny_code="APPROVAL_REQUIRED",
                        reasons=[
                            "High-risk action requires pre-authorized dual control approval from a second officer."
                        ],
                        grant_ids=contributing_grants,
                        rule="DUAL_CONTROL_APPROVAL_REQUIRED",
                        tier=tier,
                    )
                    if log_denials and session:
                        log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                    return res

            if not approval or not approval.approved_by:
                res = DecisionResult(
                    allowed=False,
                    deny_code="APPROVAL_REQUIRED",
                    reasons=[
                        "High-risk action requires pre-authorized dual control approval from a second officer."
                    ],
                    grant_ids=contributing_grants,
                    rule="DUAL_CONTROL_APPROVAL_REQUIRED",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

            if approval.requested_by != user.id:
                res = DecisionResult(
                    allowed=False,
                    deny_code="APPROVAL_REQUESTER_MISMATCH",
                    reasons=["Approval was requested by another user and cannot be claimed."],
                    grant_ids=contributing_grants,
                    rule="DUAL_CONTROL_REQUESTER_BINDING",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

            if approval.approved_by == user.id:
                res = DecisionResult(
                    allowed=False,
                    deny_code="SELF_APPROVAL_PROHIBITED",
                    reasons=["User cannot approve their own high-risk operation request."],
                    grant_ids=contributing_grants,
                    rule="DUAL_CONTROL_SELF_APPROVAL",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

            if approval.used_at is not None or approval.status == "used":
                res = DecisionResult(
                    allowed=False,
                    deny_code="APPROVAL_ALREADY_USED",
                    reasons=["Approval token has already been executed (single-use limit)."],
                    grant_ids=contributing_grants,
                    rule="DUAL_CONTROL_SINGLE_USE",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

            exp_n = to_naive_utc(approval.expires_at)
            if exp_n and at_n > exp_n:
                res = DecisionResult(
                    allowed=False,
                    deny_code="APPROVAL_EXPIRED",
                    reasons=["High-risk action approval has expired."],
                    grant_ids=contributing_grants,
                    rule="DUAL_CONTROL_TIME_WINDOW",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

            # Approver authority at use time (in scope of target job, or holding admin:override)
            approver_user = session.query(User).filter(User.id == approval.approved_by).first()
            job_scope = {"client_id": job.client_id, "branch_id": job.branch_id, "direction": job.direction, "mode": job.mode}
            approver_has_auth = (
                has_permission(session, approver_user, action, at, scope=job_scope)
                or has_permission(session, approver_user, "admin:override", at)
            )
            if not approver_user or not approver_has_auth:
                res = DecisionResult(
                    allowed=False,
                    deny_code="APPROVER_NOT_AUTHORIZED",
                    reasons=["Approver did not hold required authority in scope at execution time."],
                    grant_ids=contributing_grants,
                    rule="DUAL_CONTROL_APPROVER_AUTHORITY",
                    tier=tier,
                )
                if log_denials and session:
                    log_denial_to_audit(session, user.id, job.id, action, res.deny_code, res.reasons, at)
                return res

            consumed_approval_id = approval.id

        # -------------------------------------------------------------
        # ALL CHECKS PASSED: ALLOW
        # -------------------------------------------------------------
        if tier == "full":
            allow_reasons = [
                f"Action '{action}' is permitted for user '{user.username}' under grant(s) {contributing_grants}."
            ]
        else:
            allow_reasons = ["Action is permitted."]

        return DecisionResult(
            allowed=True,
            deny_code=None,
            reasons=allow_reasons,
            grant_ids=contributing_grants,
            rule="POLICY_ALLOW",
            tier=tier,
            approval_id=consumed_approval_id,
        )

    except Exception as exc:
        # -------------------------------------------------------------
        # STEP 8: Fail-Closed Safeguard (Q14)
        # Any unexpected error or unhandled state MUST deny access.
        # -------------------------------------------------------------
        logger.error(f"Fail-closed decision engine error: {exc}", exc_info=True)
        fail_reasons = [
            f"Fail-closed safeguard triggered: {str(exc)}"
            if is_admin_or_auditor
            else "Access denied due to an internal system safeguard."
        ]
        res = DecisionResult(
            allowed=False,
            deny_code="FAIL_CLOSED_ERROR",
            reasons=fail_reasons,
            rule="FAIL_CLOSED_SAFEGUARD",
            tier=tier if 'tier' in locals() else "generic",
        )
        if log_denials and session:
            try:
                u_id = user_or_id.id if isinstance(user_or_id, User) else str(user_or_id)
                j_id = job_or_id.id if isinstance(job_or_id, Job) else str(job_or_id)
                log_denial_to_audit(session, u_id, j_id, action, res.deny_code, res.reasons, at)
            except Exception:
                pass
        return res
