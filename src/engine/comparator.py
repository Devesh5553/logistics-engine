from datetime import datetime, timezone
from typing import Optional, Union
from sqlalchemy.orm import Session

from src.models import User, Job
from src.engine.evaluator import decide
from src.engine.types import ComparisonResult, UserEvaluationSummary


def compare_access(
    user_a_or_id: Union[User, str],
    user_b_or_id: Union[User, str],
    action: str,
    job_or_id: Union[Job, str],
    at: Optional[datetime] = None,
    session: Optional[Session] = None,
    requester_or_id: Optional[Union[User, str]] = None,
) -> ComparisonResult:
    """
    Directly answers Question 10: "Why can she do this and he can't?"
    Compares two users attempting the same action on the same job, producing
    both structured side-by-side outputs and a concise differential explanation.
    """
    if at is None:
        at = datetime.now(timezone.utc)

    if session is None:
        raise ValueError("Database session is required for compare_access()")

    # Resolve User objects
    user_a = (
        session.query(User).filter(User.id == user_a_or_id).first()
        if isinstance(user_a_or_id, str)
        else user_a_or_id
    )
    user_b = (
        session.query(User).filter(User.id == user_b_or_id).first()
        if isinstance(user_b_or_id, str)
        else user_b_or_id
    )
    job = (
        session.query(Job).filter(Job.id == job_or_id).first()
        if isinstance(job_or_id, str)
        else job_or_id
    )

    if job is None:
        raise ValueError("Job not found")
    if user_a is None or user_b is None:
        raise ValueError("Both users must exist to perform comparison")

    # Evaluate both users with full diagnostic tier for comparator
    res_a = decide(user_a, action, job, at=at, session=session, requester_or_id=requester_or_id, log_denials=False)
    res_b = decide(user_b, action, job, at=at, session=session, requester_or_id=requester_or_id, log_denials=False)

    summary_a = UserEvaluationSummary(
        user_id=user_a.id,
        username=user_a.username,
        full_name=user_a.full_name,
        allowed=res_a.allowed,
        deny_code=res_a.deny_code,
        reasons=res_a.reasons,
        grant_ids=res_a.grant_ids,
        rule=res_a.rule,
    )

    summary_b = UserEvaluationSummary(
        user_id=user_b.id,
        username=user_b.username,
        full_name=user_b.full_name,
        allowed=res_b.allowed,
        deny_code=res_b.deny_code,
        reasons=res_b.reasons,
        grant_ids=res_b.grant_ids,
        rule=res_b.rule,
    )

    # Synthesize differential plain-English summary
    if res_a.allowed and not res_b.allowed:
        if res_b.deny_code == "MAKER_CHECKER_CONFLICT":
            diff = (
                f"{user_a.full_name} is allowed because they hold a valid Checker grant and did not prepare the job. "
                f"{user_b.full_name} is denied because they prepared the checklist, triggering the Maker-Checker conflict rule."
            )
        elif res_b.deny_code == "OUT_OF_SCOPE":
            diff = (
                f"{user_a.full_name} has active grants covering this job's scope "
                f"[{job.client_id}|{job.branch_id}|{job.direction}|{job.mode}], whereas {user_b.full_name}'s grants do not match."
            )
        elif res_b.deny_code == "USER_INACTIVE":
            diff = f"{user_a.full_name} has an active account, whereas {user_b.full_name}'s account is deactivated."
        elif res_b.deny_code == "NO_PERMISSION":
            diff = f"{user_a.full_name}'s role includes '{action}', whereas {user_b.full_name}'s role does not."
        else:
            diff = f"{user_a.full_name} is permitted under rule {res_a.rule}; {user_b.full_name} is denied ({res_b.deny_code})."

    elif not res_a.allowed and res_b.allowed:
        if res_a.deny_code == "MAKER_CHECKER_CONFLICT":
            diff = (
                f"{user_b.full_name} is allowed because they did not prepare the job. "
                f"{user_a.full_name} is denied because they prepared the checklist (Maker-Checker conflict)."
            )
        elif res_a.deny_code == "OUT_OF_SCOPE":
            diff = (
                f"{user_b.full_name} has active grants matching this scope, whereas {user_a.full_name} is out of scope."
            )
        elif res_a.deny_code == "USER_INACTIVE":
            diff = f"{user_b.full_name} is active; {user_a.full_name} is deactivated."
        else:
            diff = f"{user_b.full_name} is permitted; {user_a.full_name} is denied ({res_a.deny_code})."

    elif res_a.allowed and res_b.allowed:
        diff = f"Both {user_a.full_name} and {user_b.full_name} are authorized to perform '{action}' on this job."
    else:
        diff = f"Both users are denied. {user_a.full_name}: {res_a.deny_code}. {user_b.full_name}: {res_b.deny_code}."

    return ComparisonResult(
        job_id=job.id,
        action=action,
        at=at.isoformat(),
        user_a=summary_a,
        user_b=summary_b,
        differential_summary=diff,
    )
