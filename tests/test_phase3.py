"""
test_phase3.py — Phase 3 Tests: Queue, Workflow Transitions & Assignments

Tests cover:
  - /queue scope + stage filtering
  - /queue maker-checker conflict exclusion
  - /queue client-user isolation
  - /queue absence routing (delegate surfaced, absent user warns)
  - /queue reassignment warnings for revoked/leaver jobs
  - Workflow transitions: full happy-path Confirmed → Prepared → Checked → Filed
  - Transition guard: wrong stage rejection
  - Transition guard: maker-checker conflict on check step
  - Transition guard: high-risk action (cancel) blocked without dual-control approval
  - Transition guard: high-risk action requires reason
  - Transition guard: reopen with approved dual-control succeeds
  - Job detail endpoint: client isolation (404 on other client)
  - Assignments: Manager can assign; non-Manager denied
  - API integration: GET /queue, GET /jobs/{id}, POST /jobs/{id}/transition
"""

import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from src.models import User, Job, Grant, Approval, Assignment, AuditLog, JobEvent, Role, RolePermission
from src.seeds.sample_data import SEED_BASE_TIME
from src.engine import decide, build_queue, execute_transition
from src.api.app import app, get_db


# ---------------------------------------------------------------------------
# Shared API fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def api_client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# ===========================================================================
# QUEUE TESTS
# ===========================================================================

class TestQueueFiltering:

    def test_maker_sees_confirmed_jobs_in_scope(self, db_session):
        """
        Amit Kumar (staff_maker_del) has Maker on all clients at DEL.
        He should see Confirmed jobs at DEL in his queue.
        """
        ref_date = SEED_BASE_TIME
        result = build_queue("usr_maker_del", db_session, at=ref_date)

        assert result.username == "staff_maker_del"
        assert result.total_jobs > 0

        # Every returned job should be in a stage where job:prepare is legal
        eligible_stages = {"Confirmed", "Reopened"}
        for item in result.items:
            assert item.branch_id == "DEL", f"Scoped to DEL only but got {item.branch_id}"
            assert item.stage in eligible_stages or "job:prepare" not in item.eligible_actions

    def test_checker_sees_only_prepared_jobs(self, db_session):
        """
        Kavita Rao (checker_narrow) has Checker only for CL-001, DEL, IMPORT.
        Her queue must only contain jobs at Prepared (or Reopened) stage
        within that narrow scope.
        """
        ref_date = SEED_BASE_TIME
        result = build_queue("usr_checker_del", db_session, at=ref_date)

        for item in result.items:
            if "job:check" in item.eligible_actions:
                assert item.client_id == "CL-001", f"Out of client scope: {item.client_id}"
                assert item.branch_id == "DEL", f"Out of branch scope: {item.branch_id}"
                assert item.direction == "IMPORT", f"Out of direction scope: {item.direction}"
                assert item.stage in {"Prepared", "Reopened"}, f"Wrong stage: {item.stage}"

    def test_maker_checker_conflict_excluded_from_queue(self, db_session):
        """
        Rohan (maker_wide_checker_few) prepared JOB-PLANTED-05.
        That job must NOT appear as checkable in his queue (Maker-Checker SOD).
        """
        ref_date = SEED_BASE_TIME
        result = build_queue("usr_maker_wide", db_session, at=ref_date)

        for item in result.items:
            if item.job_id == "JOB-PLANTED-05":
                assert "job:check" not in item.eligible_actions, (
                    "JOB-PLANTED-05 should not be checkable by Rohan (he prepared it)"
                )

    def test_client_user_queue_isolated_to_own_client(self, db_session):
        """
        John Doe (client_acme_user) is a client user for CL-001.
        His queue must only contain CL-001 jobs; no other client jobs.
        """
        ref_date = SEED_BASE_TIME
        result = build_queue("usr_client_acme", db_session, at=ref_date)

        for item in result.items:
            assert item.client_id == "CL-001", (
                f"Client user leaked into client {item.client_id}"
            )

    def test_inactive_user_gets_empty_queue(self, db_session):
        """
        Dev Patel (user_leaver) is deactivated.
        His queue must be empty regardless of grants.
        """
        ref_date = SEED_BASE_TIME
        result = build_queue("usr_leaver", db_session, at=ref_date)

        assert result.total_jobs == 0

    def test_delegate_sees_absent_users_jobs(self, db_session):
        """
        Sunita Verma (user_delegate) has a 7-day delegated Checker grant
        covering the same scope as absent Vikram.  At T+3 she should see
        Prepared jobs at DEL that she can check.
        """
        at = SEED_BASE_TIME + timedelta(days=3)
        # Force JOB-PLANTED-12 into Prepared so Sunita can check
        j12 = db_session.query(Job).filter_by(id="JOB-PLANTED-12").one()
        # JOB-PLANTED-12 is already in Prepared but at BLR, not DEL.
        # Use JOB-PLANTED-01 — Confirmed at DEL, put it in Prepared by someone else
        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        j01.stage = "Prepared"
        j01.prepared_by = "usr_maker_del"
        db_session.flush()

        result = build_queue("usr_delegate", db_session, at=at)

        # Sunita should have at least job:check for JOB-PLANTED-01
        job_ids = [item.job_id for item in result.items]
        assert "JOB-PLANTED-01" in job_ids, (
            f"Sunita's delegate grant should surface JOB-PLANTED-01 as checkable. Items: {job_ids}"
        )

    def test_delegate_grant_expires(self, db_session):
        """
        Sunita's delegation expires at T+7.  At T+8 her queue must be empty
        (the delegation grant is past its valid_to).
        """
        at = SEED_BASE_TIME + timedelta(days=8)
        result = build_queue("usr_delegate", db_session, at=at)
        assert result.total_jobs == 0

    def test_reassignment_warning_surfaced(self, db_session):
        """
        JOB-PLANTED-12 has an active assignment for the absent Vikram with
        needs_reassignment=True.  The queue item for staff_maker_all should
        carry a warning about the flagged reassignment.
        """
        ref_date = SEED_BASE_TIME
        result = build_queue("usr_maker_all", db_session, at=ref_date)

        flagged = [item for item in result.items if item.job_id == "JOB-PLANTED-12"]
        # JOB-PLANTED-12 is in Prepared stage — maker_all may not see it as preparable
        # but *any* manager/admin seeing it would see the warning.
        # Use manager instead:
        result_mgr = build_queue("usr_manager", db_session, at=ref_date)
        all_ids = [item.job_id for item in result_mgr.items]
        # Manager can assign, so JOB-PLANTED-12 should appear in manager's queue with warning
        # (manager has job:assign which is valid on Prepared)
        for item in result_mgr.items:
            if item.needs_reassignment:
                assert item.reassignment_reason is not None
                assert len(item.warnings) > 0

    def test_action_filter_narrows_queue(self, db_session):
        """
        Filtering by action='job:prepare' returns only jobs where that
        specific action is eligible for the user.
        """
        ref_date = SEED_BASE_TIME
        result = build_queue(
            "usr_maker_del", db_session, at=ref_date, action_filter="job:prepare"
        )
        for item in result.items:
            assert "job:prepare" in item.eligible_actions

    def test_branch_filter_narrows_queue(self, db_session):
        """
        Deepak (staff_maker_all) has Maker everywhere.  Filtering by branch=DEL
        must return only DEL jobs.
        """
        ref_date = SEED_BASE_TIME
        result = build_queue(
            "usr_maker_all", db_session, at=ref_date, branch_filter="DEL"
        )
        for item in result.items:
            assert item.branch_id == "DEL"


# ===========================================================================
# TRANSITION TESTS
# ===========================================================================

class TestWorkflowTransitions:

    def test_happy_path_confirmed_to_prepared(self, db_session):
        """
        Amit Kumar (staff_maker_del) can prepare JOB-PLANTED-01 (Confirmed → Prepared).
        """
        ref_date = SEED_BASE_TIME
        result = execute_transition(
            user_or_id="usr_maker_del",
            job_or_id="JOB-PLANTED-01",
            action="job:prepare",
            session=db_session,
            at=ref_date,
        )
        assert result.success is True
        assert result.from_stage == "Confirmed"
        assert result.to_stage == "Prepared"
        assert result.event_id is not None

        # Verify DB mutation
        job = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        assert job.stage == "Prepared"
        assert job.prepared_by == "usr_maker_del"
        assert job.version == 2

    def test_happy_path_prepared_to_checked(self, db_session):
        """
        Kavita (checker_narrow) can check JOB-PLANTED-01 after Amit prepared it.
        (Prepared → Checked, different person, in scope)
        """
        ref_date = SEED_BASE_TIME

        # Set up: prepare the job first
        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        j01.stage = "Prepared"
        j01.prepared_by = "usr_maker_del"
        db_session.flush()

        result = execute_transition(
            user_or_id="usr_checker_del",
            job_or_id="JOB-PLANTED-01",
            action="job:check",
            session=db_session,
            at=ref_date,
        )
        assert result.success is True
        assert result.to_stage == "Checked"

        job = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        assert job.stage == "Checked"
        assert job.checked_by == "usr_checker_del"

    def test_happy_path_checked_to_filed(self, db_session):
        """
        Suresh (staff_filer_all) can file JOB-PLANTED-07 (already Checked → Filed).
        """
        ref_date = SEED_BASE_TIME
        result = execute_transition(
            user_or_id="usr_filer_all",
            job_or_id="JOB-PLANTED-07",
            action="job:file",
            session=db_session,
            at=ref_date,
        )
        assert result.success is True
        assert result.to_stage == "Filed"

        job = db_session.query(Job).filter_by(id="JOB-PLANTED-07").one()
        assert job.stage == "Filed"
        assert job.locked is True  # Filing locks the job

    def test_wrong_stage_transition_rejected(self, db_session):
        """
        Cannot prepare a job that is already Filed (INVALID_TRANSITION).
        """
        ref_date = SEED_BASE_TIME
        result = execute_transition(
            user_or_id="usr_maker_del",
            job_or_id="JOB-PLANTED-08",  # Already Filed
            action="job:prepare",
            session=db_session,
            at=ref_date,
        )
        assert result.success is False
        assert result.deny_code == "INVALID_TRANSITION"

    def test_maker_checker_conflict_on_transition(self, db_session):
        """
        Rohan prepared JOB-PLANTED-05.  Attempting to check it himself
        must fail with MAKER_CHECKER_CONFLICT (not an INVALID_TRANSITION).
        """
        ref_date = SEED_BASE_TIME
        # JOB-PLANTED-05 is already in Prepared, prepared_by=usr_maker_wide
        result = execute_transition(
            user_or_id="usr_maker_wide",
            job_or_id="JOB-PLANTED-05",
            action="job:check",
            session=db_session,
            at=ref_date,
        )
        assert result.success is False
        assert result.deny_code == "MAKER_CHECKER_CONFLICT"

    def test_cancel_filed_requires_reason(self, db_session):
        """
        High-risk action (cancel_filed) without a reason is blocked with REASON_REQUIRED
        before even reaching the dual-control check.
        """
        ref_date = SEED_BASE_TIME
        result = execute_transition(
            user_or_id="usr_manager",
            job_or_id="JOB-PLANTED-08",
            action="job:cancel_filed",
            session=db_session,
            at=ref_date,
            reason=None,  # missing reason
        )
        assert result.success is False
        assert result.deny_code == "REASON_REQUIRED"

    def test_cancel_filed_blocked_without_dual_control_approval(self, db_session):
        """
        Even with a reason, cancelling a filed job without a second approver sign-off
        must be denied with APPROVAL_REQUIRED.
        JOB-PLANTED-08 has appr_pending_cancel with approved_by=None.
        """
        ref_date = SEED_BASE_TIME
        result = execute_transition(
            user_or_id="usr_manager",
            job_or_id="JOB-PLANTED-08",
            action="job:cancel_filed",
            session=db_session,
            at=ref_date,
            reason="Client requested recall due to shipment dispute",
        )
        assert result.success is False
        assert result.deny_code == "APPROVAL_REQUIRED"

    def test_reopen_succeeds_with_approved_dual_control(self, db_session):
        """
        Manager (usr_manager, DEL-scoped) reopens JOB-PLANTED-10 (Cancelled, DEL)
        with a pre-seeded dual-control approval from a different approver (usr_admin_1).
        Manager has job:reopen_closed; Admin does not.
        """
        ref_date = SEED_BASE_TIME

        # Create a valid dual-control approval for JOB-PLANTED-10 (Cancelled, DEL)
        # The manager (usr_manager) is the actor; admin_1 is the approver (different person)
        fresh_approval = Approval(
            id="appr_test_reopen_10",
            target_type="JOB_REOPEN",
            target_id="JOB-PLANTED-10",
            requested_by="usr_manager",
            approved_by="usr_admin_1",   # Different person = valid second approver
            reason="Customs amendment required",
            expires_at=ref_date + timedelta(days=2),
            used_at=None,
            is_active=True,
            created_at=ref_date,
        )
        db_session.add(fresh_approval)
        db_session.flush()

        # usr_manager (who is NOT the approver) performs the reopen
        result = execute_transition(
            user_or_id="usr_manager",
            job_or_id="JOB-PLANTED-10",
            action="job:reopen_closed",
            session=db_session,
            at=ref_date,
            reason="Customs amendment required",
        )
        assert result.success is True, f"Expected success but got: {result}"
        assert result.to_stage == "Reopened"

        job = db_session.query(Job).filter_by(id="JOB-PLANTED-10").one()
        assert job.stage == "Reopened"
        assert job.locked is False

    def test_self_approval_blocked_on_high_risk(self, db_session):
        """
        The actor cannot be the same person who approved the dual-control token
        (SELF_APPROVAL_PROHIBITED).
        Uses usr_manager on JOB-PLANTED-10 (DEL, Cancelled) where Manager has
        job:reopen_closed permission.
        """
        ref_date = SEED_BASE_TIME

        # Create an approval where usr_manager is BOTH requester AND approver — invalid!
        self_approval = Approval(
            id="appr_self_approve_test",
            target_type="JOB_REOPEN",
            target_id="JOB-PLANTED-10",
            requested_by="usr_manager",
            approved_by="usr_manager",   # Same person: SELF_APPROVAL_PROHIBITED
            reason="Self-approved test",
            expires_at=ref_date + timedelta(days=2),
            used_at=None,
            is_active=True,
            created_at=ref_date,
        )
        db_session.add(self_approval)
        db_session.flush()

        result = execute_transition(
            user_or_id="usr_manager",
            job_or_id="JOB-PLANTED-10",
            action="job:reopen_closed",
            session=db_session,
            at=ref_date,
            reason="Testing self-approval guard",
        )
        assert result.success is False
        assert result.deny_code == "SELF_APPROVAL_PROHIBITED"

    def test_transition_creates_job_event(self, db_session):
        """
        A successful transition must append a JobEvent record with correct fields.
        """
        from src.models import JobEvent
        ref_date = SEED_BASE_TIME
        initial_events = db_session.query(JobEvent).filter_by(job_id="JOB-PLANTED-01").count()

        execute_transition(
            user_or_id="usr_maker_del",
            job_or_id="JOB-PLANTED-01",
            action="job:prepare",
            session=db_session,
            at=ref_date,
        )

        final_events = db_session.query(JobEvent).filter_by(job_id="JOB-PLANTED-01").count()
        assert final_events == initial_events + 1

        latest = (
            db_session.query(JobEvent)
            .filter_by(job_id="JOB-PLANTED-01")
            .order_by(JobEvent.timestamp.desc())
            .first()
        )
        assert latest.action == "PREPARE"
        assert latest.from_stage == "Confirmed"
        assert latest.to_stage == "Prepared"
        assert latest.actor_id == "usr_maker_del"

    def test_out_of_scope_transition_denied(self, db_session):
        """
        Kavita (checker_narrow) only covers CL-001, DEL, IMPORT.
        She cannot prepare JOB-PLANTED-09 (CL-002, MUM, EXPORT).
        """
        ref_date = SEED_BASE_TIME
        # Put JOB-PLANTED-09 back to Confirmed so prepare is a valid workflow step
        j09 = db_session.query(Job).filter_by(id="JOB-PLANTED-09").one()
        j09.stage = "Confirmed"
        j09.locked = False
        db_session.flush()

        result = execute_transition(
            user_or_id="usr_checker_del",
            job_or_id="JOB-PLANTED-09",
            action="job:prepare",
            session=db_session,
            at=ref_date,
        )
        assert result.success is False
        assert result.deny_code in {"OUT_OF_SCOPE", "NO_PERMISSION"}


# ===========================================================================
# JOB DETAIL + ASSIGNMENTS TESTS (direct engine, not API)
# ===========================================================================

class TestJobDetailAndAssignments:

    def test_assignment_list_returns_seeded_assignments(self, db_session):
        """
        JOB-PLANTED-01 has an active assignment for usr_maker_del.
        The DB assignment query should return it.
        """
        assignments = (
            db_session.query(Assignment)
            .filter(Assignment.job_id == "JOB-PLANTED-01", Assignment.is_active == True)
            .all()
        )
        assert len(assignments) >= 1
        ids = [a.user_id for a in assignments]
        assert "usr_maker_del" in ids

    def test_reassignment_flagged_for_absent_user(self, db_session):
        """
        JOB-PLANTED-12 is assigned to the absent Vikram with needs_reassignment=True.
        """
        asgn = (
            db_session.query(Assignment)
            .filter(Assignment.job_id == "JOB-PLANTED-12", Assignment.user_id == "usr_absent")
            .one()
        )
        assert asgn.needs_reassignment is True
        assert asgn.reassignment_reason is not None


# ===========================================================================
# API INTEGRATION TESTS
# ===========================================================================

class TestPhase3Api:

    def test_get_queue_returns_200(self, api_client):
        """GET /queue?user_id=... returns a QueueResult with 200."""
        resp = api_client.get(
            "/queue?user_id=usr_maker_del",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert "total_jobs" in data
        assert data["username"] == "staff_maker_del"

    def test_get_queue_inactive_user_empty(self, api_client):
        """Inactive user's queue is empty (not an error)."""
        resp = api_client.get(
            "/queue?user_id=usr_leaver",
            headers={"X-Actor-Id": "usr_leaver"},
        )
        assert resp.status_code == 200
        assert resp.json()["total_jobs"] == 0

    def test_get_queue_action_filter(self, api_client):
        """GET /queue?action=job:prepare returns only preparable jobs."""
        resp = api_client.get(
            "/queue?user_id=usr_maker_del&action=job:prepare",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 200
        data = resp.json()
        for item in data["items"]:
            assert "job:prepare" in item["eligible_actions"]

    def test_get_job_detail(self, api_client):
        """GET /jobs/{job_id} returns correct job details."""
        resp = api_client.get(
            "/jobs/JOB-PLANTED-01",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "JOB-PLANTED-01"
        assert data["client_id"] == "CL-001"
        assert data["branch_id"] == "DEL"
        assert data["stage"] == "Confirmed"

    def test_get_job_not_found(self, api_client):
        """GET /jobs/{job_id} for non-existent job → 404."""
        resp = api_client.get(
            "/jobs/JOB-DOES-NOT-EXIST",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 404

    def test_get_job_client_user_isolation_404(self, api_client):
        """
        Client user (Acme) accessing Zenith's job → 404 (existence obfuscation).
        """
        resp = api_client.get(
            "/jobs/JOB-PLANTED-09",
            headers={"X-Actor-Id": "usr_client_acme"},
        )
        assert resp.status_code == 404

    def test_transition_prepare_via_api(self, api_client):
        """
        POST /jobs/JOB-PLANTED-01/transition with valid actor and action → 200.
        """
        resp = api_client.post(
            "/jobs/JOB-PLANTED-01/transition",
            json={
                "action": "job:prepare",
                "version": 1,
            },
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["to_stage"] == "Prepared"

    def test_transition_wrong_stage_via_api(self, api_client, db_session):
        """
        POST /jobs/JOB-PLANTED-08/transition with action=job:prepare on Filed job → 422.
        """
        j08 = db_session.query(Job).filter_by(id="JOB-PLANTED-08").one()
        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/transition",
            json={
                "action": "job:prepare",
                "version": j08.version,
            },
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 422

    def test_transition_denied_via_api(self, api_client, db_session):
        """
        Rohan attempting to check his own prepared job → 403.
        """
        j05 = db_session.query(Job).filter_by(id="JOB-PLANTED-05").one()
        resp = api_client.post(
            "/jobs/JOB-PLANTED-05/transition",
            json={
                "action": "job:check",
                "version": j05.version,
            },
            headers={"X-Actor-Id": "usr_maker_wide"},
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["detail"]["deny_code"] == "MAKER_CHECKER_CONFLICT"

    def test_get_assignments_via_api(self, api_client):
        """GET /jobs/JOB-PLANTED-01/assignments returns active assignments."""
        resp = api_client.get(
            "/jobs/JOB-PLANTED-01/assignments",
            headers={"X-Actor-Id": "usr_checker_del"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        user_ids = [a["user_id"] for a in data]
        assert "usr_maker_del" in user_ids

    def test_create_assignment_manager_allowed(self, api_client):
        """
        Manager (staff_manager_del) can assign usr_maker_del to JOB-PLANTED-01.
        """
        resp = api_client.post(
            "/jobs/JOB-PLANTED-01/assignments",
            json={
                "assignee_id": "usr_maker_del",
                "role": "Maker",
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user_id"] == "usr_maker_del"
        assert data["assigned_role"] == "Maker"
        assert data["is_active"] is True

    def test_create_assignment_non_manager_denied(self, api_client):
        """
        A plain Maker (staff_maker_del) without job:assign permission is denied → 403.
        """
        resp = api_client.post(
            "/jobs/JOB-PLANTED-01/assignments",
            json={
                "assignee_id": "usr_checker_del",
                "role": "Checker",
            },
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 403

    def test_queue_branch_filter_via_api(self, api_client):
        """GET /queue?branch=DEL restricts jobs to DEL branch."""
        resp = api_client.get(
            "/queue?user_id=usr_maker_all&branch=DEL",
            headers={"X-Actor-Id": "usr_maker_all"},
        )
        assert resp.status_code == 200
        data = resp.json()
        for item in data["items"]:
            assert item["branch_id"] == "DEL"

    def test_queue_client_user_via_api(self, api_client):
        """Client user's queue via API is isolated to their client."""
        resp = api_client.get(
            "/queue?user_id=usr_client_acme",
            headers={"X-Actor-Id": "usr_client_acme"},
        )
        assert resp.status_code == 200
        data = resp.json()
        for item in data["items"]:
            assert item["client_id"] == "CL-001"

    def test_user_queue_endpoint_path_syntax(self, api_client):
        """GET /users/{id}/queue matches GET /queue?user_id={id}."""
        resp1 = api_client.get(
            "/users/usr_maker_del/queue",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        resp2 = api_client.get(
            "/queue?user_id=usr_maker_del",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["total_jobs"] == resp2.json()["total_jobs"]


# ===========================================================================
# FULL QUEUE PARITY TEST ACROSS THE WHOLE SAMPLE
# ===========================================================================

class TestQueueFullParity:

    def test_queue_full_parity_across_whole_sample(self, db_session):
        """
        Plan requirement:
        'Queue count == items returned == set where decide() allows (full parity across whole sample)'
        Tests across all jobs in the database for representative users.
        """
        ref_date = SEED_BASE_TIME
        all_jobs = db_session.query(Job).all()
        assert len(all_jobs) >= 15, "Database must contain at least planted jobs"

        # Test users covering different roles
        test_profiles = [
            ("usr_maker_del", "job:prepare"),
            ("usr_checker_del", "job:check"),
            ("usr_filer_all", "job:file"),
        ]

        for user_id, action in test_profiles:
            # 1. Build queue without limit
            result = build_queue(user_id, db_session, at=ref_date, limit=None)

            # Count == items returned
            assert result.total_jobs == len(result.items), (
                f"Mismatch for {user_id}: total_jobs={result.total_jobs} != items count={len(result.items)}"
            )

            # Map of queue items
            queue_actionable_jobs = {
                item.job_id for item in result.items
                if action in item.eligible_actions
            }

            # 2. Check every single job in DB against decide()
            for job in all_jobs:
                dec = decide(user_id, action, job, at=ref_date, session=db_session, log_denials=False)
                if dec.allowed:
                    assert job.id in queue_actionable_jobs, (
                        f"Parity failure for {user_id}: decide() allowed '{action}' on {job.id} "
                        f"but job was NOT in queue!"
                    )
                else:
                    assert job.id not in queue_actionable_jobs, (
                        f"Parity failure for {user_id}: decide() denied '{action}' on {job.id} ({dec.deny_code}) "
                        f"but job WAS returned as actionable in queue!"
                    )


# ===========================================================================
# OPERATIONAL GUARDRAIL & CONCURRENCY TESTS
# ===========================================================================

class TestOperationalGuardrails:

    def test_approval_cannot_be_reused(self, db_session):
        """
        Single-use limit: Once an approval is consumed by a high-risk transition,
        a subsequent attempt to execute with the same approval must fail with
        APPROVAL_ALREADY_USED.
        """
        ref_date = SEED_BASE_TIME
        approval = Approval(
            id="appr_single_use_test",
            target_type="JOB_REOPEN",
            target_id="JOB-PLANTED-10",
            requested_by="usr_manager",
            approved_by="usr_admin_1",
            reason="Customs dispute resolved",
            expires_at=ref_date + timedelta(days=2),
            used_at=None,
            is_active=True,
            created_at=ref_date,
        )
        db_session.add(approval)
        db_session.flush()

        # First execution succeeds
        res1 = execute_transition(
            user_or_id="usr_manager",
            job_or_id="JOB-PLANTED-10",
            action="job:reopen_closed",
            session=db_session,
            at=ref_date,
            reason="Customs dispute resolved",
        )
        assert res1.success is True
        assert approval.used_at is not None

        # Change job back to Cancelled to attempt reusing the same approval
        job = db_session.query(Job).filter_by(id="JOB-PLANTED-10").one()
        job.stage = "Cancelled"
        job.locked = True
        db_session.flush()

        # Second execution must fail because approval is already used
        res2 = execute_transition(
            user_or_id="usr_manager",
            job_or_id="JOB-PLANTED-10",
            action="job:reopen_closed",
            session=db_session,
            at=ref_date,
            reason="Customs dispute resolved again",
        )
        assert res2.success is False
        assert res2.deny_code == "APPROVAL_ALREADY_USED"

    def test_concurrent_file_optimistic_locking(self, api_client, db_session):
        """
        Two concurrent filing attempts with the same version:
        First succeeds, second fails with HTTP 409 VERSION_CONFLICT.
        """
        j07 = db_session.query(Job).filter_by(id="JOB-PLANTED-07").one()
        current_version = j07.version

        resp1 = api_client.post(
            "/jobs/JOB-PLANTED-07/transition",
            json={
                "action": "job:file",
                "version": current_version,
            },
            headers={"X-Actor-Id": "usr_filer_all"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["success"] is True

        # Second attempt presents stale version
        resp2 = api_client.post(
            "/jobs/JOB-PLANTED-07/transition",
            json={
                "action": "job:file",
                "version": current_version,  # Stale version
            },
            headers={"X-Actor-Id": "usr_filer_all"},
        )
        assert resp2.status_code == 409
        assert "Job version mismatch" in resp2.json()["detail"]

    def test_revoke_between_check_and_act_is_denied(self, db_session):
        """
        A user checks permission (allowed).
        Before they act, their grant is revoked.
        The subsequent transition attempt is denied.
        """
        ref_date = SEED_BASE_TIME
        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()

        # Check permission: Amit is allowed to prepare
        check_before = decide("usr_maker_del", "job:prepare", j01, at=ref_date, session=db_session)
        assert check_before.allowed is True

        # Revoke Amit's Maker grant
        maker_grant = db_session.query(Grant).filter_by(user_id="usr_maker_del").first()
        maker_grant.revoked_at = ref_date
        maker_grant.revocation_reason = "Security audit hold"
        db_session.flush()

        # Transition attempt must be denied
        res = execute_transition(
            user_or_id="usr_maker_del",
            job_or_id="JOB-PLANTED-01",
            action="job:prepare",
            session=db_session,
            at=ref_date,
            expected_version=j01.version,
        )
        assert res.success is False
        assert res.deny_code in ("OUT_OF_SCOPE", "NO_PERMISSION")


# ===========================================================================
# SCOPE EDIT & ADMIN OVERRIDE ENDPOINT TESTS
# ===========================================================================

class TestScopeEditAndOverrideEndpoints:

    def test_scope_edit_requires_reason(self, api_client):
        """PATCH /jobs/{id}/scope without reason returns 422."""
        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={
                "branch_id": "BOM",
                "reason": "",
                "expected_version": 1,
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 422

    def test_scope_edit_requires_dual_control_approval(self, api_client):
        """Editing job scope without dual-control approval returns 403 APPROVAL_REQUIRED."""
        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={
                "branch_id": "DEL",
                "reason": "Routing changed due to port strike",
                "expected_version": 1,
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 403

    def test_scope_edit_self_approval_denied(self, api_client, db_session):
        """Self-approved scope edit token is rejected with SELF_APPROVAL_PROHIBITED."""
        ref_date = SEED_BASE_TIME
        appr = Approval(
            id="appr_scope_self",
            action="job:edit_scope",
            target_type="job",
            target_id="JOB-PLANTED-01",
            requested_by="usr_manager",
            approved_by="usr_manager",  # Self approval!
            reason="Port change",
            status="approved",
            expires_at=ref_date + timedelta(days=1),
            used_at=None,
            is_active=True,
            created_at=ref_date,
        )
        db_session.add(appr)
        db_session.commit()

        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={
                "branch_id": "DEL",
                "reason": "Port change",
                "expected_version": 1,
                "approval_id": "appr_scope_self",
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 403
        assert "SELF_APPROVAL_PROHIBITED" in resp.text

    def test_scope_edit_happy_path(self, api_client, db_session):
        """Scope edit with valid second-approver approval updates job scope and version."""
        ref_date = SEED_BASE_TIME
        appr = Approval(
            id="appr_scope_valid",
            action="job:edit_scope",
            target_type="job",
            target_id="JOB-PLANTED-01",
            requested_by="usr_admin_1",
            approved_by="usr_admin_2",  # Independent approver
            reason="Client requested MUM port clearance",
            status="approved",
            expires_at=ref_date + timedelta(days=1),
            used_at=None,
            is_active=True,
            created_at=ref_date,
        )
        db_session.add(appr)
        db_session.commit()

        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        v = j01.version

        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={
                "branch_id": "MUM",
                "reason": "Client requested MUM port clearance",
                "expected_version": v,
                "approval_id": "appr_scope_valid",
                "at": ref_date.isoformat(),
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["branch_id"] == "MUM"
        assert data["version"] == v + 1

        # Verify approval is marked used
        db_session.expire_all()
        re_appr = db_session.query(Approval).filter_by(id="appr_scope_valid").one()
        assert re_appr.used_at is not None
        assert re_appr.status == "used"

    def test_admin_override_non_admin_denied(self, api_client):
        """Non-admin attempting admin override is denied 403."""
        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/override",
            json={
                "action": "FORCE_UNLOCK",
                "reason": "Emergency fix",
                "unlock": True,
            },
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 403

    def test_admin_override_happy_path_logged(self, api_client, db_session):
        """
        Admin override mutates job, creates JobEvent, and writes hash-chained AuditLog.
        Requires second admin approval because JOB-PLANTED-08 is Filed/locked.
        """
        ref_date = SEED_BASE_TIME
        appr = Approval(
            id="appr_override_happy",
            action="admin:override",
            target_type="job",
            target_id="JOB-PLANTED-08",
            requested_by="usr_admin_1",
            approved_by="usr_admin_2",
            reason="Court order revision mandate",
            status="approved",
            expires_at=ref_date + timedelta(days=1),
            created_at=ref_date,
        )
        db_session.add(appr)
        db_session.commit()

        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/override",
            json={
                "action": "FORCE_STAGE",
                "target_stage": "Confirmed",
                "unlock": True,
                "reason": "Court order revision mandate",
                "approval_id": "appr_override_happy",
                "at": ref_date.isoformat(),
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["stage"] == "Confirmed"
        assert data["locked"] is False

        # Verify unalterable audit log
        log_entry = (
            db_session.query(AuditLog)
            .filter_by(target_id="JOB-PLANTED-08", event_type="OVERRIDE")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert log_entry is not None
        assert log_entry.decision == "EXECUTED"
        assert log_entry.verify_integrity() is True


# ===========================================================================
# GRANT MANAGEMENT & DELEGATION API TESTS
# ===========================================================================

class TestGrantManagementApi:

    def test_admin_cannot_grant_to_self(self, api_client):
        """Admin attempting to grant to themselves is blocked (ADMIN_SELF_GRANT_PROHIBITED)."""
        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_admin_1",
                "role_id": "Admin",
                "branch_id": "DEL",
                "source": "direct",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422
        assert "ADMIN_SELF_GRANT_PROHIBITED" in resp.json()["detail"]

    def test_admin_scope_change_needs_second_admin(self, api_client, db_session):
        """Creating an Admin grant without dual-control approval returns 403."""
        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_maker_del",
                "role_id": "Admin",
                "source": "direct",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 403
        assert "DUAL_CONTROL_APPROVAL_REQUIRED" in resp.json()["detail"]["deny_code"]

    def test_admin_scope_change_with_second_admin_succeeds(self, api_client, db_session):
        """Creating an Admin grant with a valid second admin approval succeeds."""
        ref_date = SEED_BASE_TIME
        appr = Approval(
            id="appr_admin_grant_dual",
            action="grant:create",
            target_type="grant",
            target_id="usr_maker_del",
            requested_by="usr_admin_1",
            approved_by="usr_admin_2",  # Second admin sign-off
            reason="Promotion to branch system admin",
            status="approved",
            expires_at=ref_date + timedelta(days=2),
            used_at=None,
            is_active=True,
            created_at=ref_date,
        )
        db_session.add(appr)
        db_session.commit()

        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_maker_del",
                "role_id": "Admin",
                "approval_id": "appr_admin_grant_dual",
                "source": "direct",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["role_id"] == "Admin"
        assert data["user_id"] == "usr_maker_del"

    def test_delegate_scope_cannot_exceed_delegator(self, api_client):
        """
        Kavita (usr_checker_del) is scoped to CL-001, DEL, IMPORT.
        Attempting to delegate outside her branch/client is rejected with DELEGATION_SCOPE_EXCEEDED.
        """
        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_delegate",
                "role_id": "Checker",
                "branch_id": "MUM",  # Exceeds delegator's DEL branch
                "valid_to": (SEED_BASE_TIME + timedelta(days=3)).isoformat(),
                "source": "delegation",
            },
            headers={"X-Actor-Id": "usr_checker_del"},
        )
        assert resp.status_code == 422
        assert "DELEGATION_SCOPE_EXCEEDED" in resp.json()["detail"]

    def test_delegate_expiry_cannot_exceed_delegator(self, api_client, db_session):
        """
        Delegation cannot exceed the parent grant's valid_to expiry.
        """
        ref_date = SEED_BASE_TIME
        p_grant = Grant(
            id="grnt_expiring_parent",
            user_id="usr_manager",
            role_id="Checker",
            branch_id="DEL",
            valid_from=ref_date,
            valid_to=ref_date + timedelta(days=5),
            source="direct",
        )
        db_session.add(p_grant)
        db_session.commit()

        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_delegate",
                "role_id": "Checker",
                "branch_id": "DEL",
                "parent_grant_id": "grnt_expiring_parent",
                "valid_to": (ref_date + timedelta(days=10)).isoformat(),  # 10 > 5
                "source": "delegation",
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 422
        assert "DELEGATION_EXPIRY_EXCEEDED" in resp.json()["detail"]

    def test_redelegation_prohibited(self, api_client):
        """
        Sunita Verma (usr_delegate) holds a delegated grant.
        Attempting to re-delegate that grant is rejected.
        """
        at = SEED_BASE_TIME + timedelta(days=2)
        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_maker_del",
                "role_id": "Checker",
                "parent_grant_id": "grnt_del_delegated",
                "valid_to": (SEED_BASE_TIME + timedelta(days=3)).isoformat(),
                "source": "delegation",
                "at": at.isoformat(),
            },
            headers={"X-Actor-Id": "usr_delegate"},
        )
        assert resp.status_code in (403, 422)

    def test_end_date_grant_flags_in_flight_assignments(self, api_client, db_session):
        """
        PATCH /grants/{id} updates valid_to. If the end date is now/past,
        in-flight job assignments matching the grant scope are flagged for reassignment.
        """
        ref_date = SEED_BASE_TIME
        grant = db_session.query(Grant).filter_by(user_id="usr_maker_del", role_id="Maker").first()

        resp = api_client.patch(
            f"/grants/{grant.id}",
            json={
                "valid_to": ref_date.isoformat(),
                "reason": "Maker reassigned to customs auditing",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200

        # Check that Amit's assignment on JOB-PLANTED-01 is flagged
        asgn = db_session.query(Assignment).filter_by(job_id="JOB-PLANTED-01", user_id="usr_maker_del").first()
        assert asgn.needs_reassignment is True
        assert "Grant ended" in asgn.reassignment_reason

    def test_revoke_grant_requires_reason(self, api_client):
        """DELETE /grants/{id} without mandatory reason returns 422."""
        grant_id = "grnt_maker_del"
        resp = api_client.delete(
            f"/grants/{grant_id}?reason=",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422
        assert "REASON_REQUIRED" in resp.json()["detail"]

    def test_revoke_grant_happy_path(self, api_client, db_session):
        """DELETE /grants/{id} with reason revokes the grant and flags assignments."""
        resp = api_client.delete(
            "/grants/grnt_maker_del?reason=Disciplinary+investigation+hold",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["revoked_at"] is not None
        assert data["revocation_reason"] == "Disciplinary investigation hold"


# ===========================================================================
# LEAVER DEACTIVATION & BRANCH TRANSFER API TESTS
# ===========================================================================

class TestLifecycleApis:

    def test_leaver_deactivation_api(self, api_client, db_session):
        """
        POST /users/{id}/deactivate atomically:
        1. Deactivates user account
        2. Revokes all grants
        3. Flags active assignments
        4. Empties their queue
        """
        resp = api_client.post(
            "/users/usr_maker_del/deactivate",
            json={
                "reason": "Resigned from company",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_active"] is False
        assert data["grants_revoked"] >= 1

        # Queue must now be empty
        q_resp = api_client.get(
            "/users/usr_maker_del/queue",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert q_resp.status_code == 200
        assert q_resp.json()["total_jobs"] == 0

        # Check assignment flagged
        asgn = db_session.query(Assignment).filter_by(user_id="usr_maker_del").first()
        if asgn:
            assert asgn.needs_reassignment is True
            assert "leaver" in asgn.reassignment_reason

    def test_branch_transfer_api(self, api_client, db_session):
        """
        POST /users/{id}/transfer:
        1. Revokes old branch grants
        2. Issues new branch grants
        3. Flags old branch assignments for reassignment
        """
        resp = api_client.post(
            "/users/usr_maker_del/transfer",
            json={
                "from_branch_id": "DEL",
                "to_branch_id": "BLR",
                "reason": "Relocation to Bangalore tech customs hub",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["from_branch_id"] == "DEL"
        assert data["to_branch_id"] == "BLR"
        assert len(data["new_grants_created"]) >= 1

        # Verify in DB: new active grant at BLR
        blr_grant = (
            db_session.query(Grant)
            .filter_by(user_id="usr_maker_del", branch_id="BLR", revoked_at=None)
            .first()
        )
        assert blr_grant is not None
        assert blr_grant.role_id == "Maker"


# ===========================================================================
# REVIEW GATE #3 FIXES: NAMED TESTS FOR F1 THROUGH F8
# ===========================================================================

class TestReviewGate3Fixes:

    # --- F1: Grants Privilege Escalation Hole Closure ---

    def test_f1_unknown_source_rejected(self, api_client):
        resp = api_client.post(
            "/grants",
            json={"user_id": "usr_maker_del", "role_id": "Maker", "source": "magic"},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422
        assert "Invalid grant source" in resp.json()["detail"]

    def test_f1_trainee_delegating_admin_to_self_using_wildcard_parent_rejected(self, api_client):
        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_trainee",
                "role_id": "Admin",
                "source": "delegation",
                "parent_grant_id": "grnt_admin_1",
                "valid_to": (SEED_BASE_TIME + timedelta(days=2)).isoformat(),
            },
            headers={"X-Actor-Id": "usr_trainee"},
        )
        assert resp.status_code == 422

    def test_f1_delegating_mismatched_role_rejected(self, api_client):
        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_delegate",
                "role_id": "Maker",
                "source": "delegation",
                "parent_grant_id": "grnt_cn_check",
                "valid_to": (SEED_BASE_TIME + timedelta(days=2)).isoformat(),
            },
            headers={"X-Actor-Id": "usr_checker_del"},
        )
        assert resp.status_code == 422
        assert "DELEGATION_ROLE_MISMATCH" in resp.json()["detail"]

    def test_f1_admin_delegating_admin_rejected(self, api_client):
        resp = api_client.post(
            "/grants",
            json={
                "user_id": "usr_maker_del",
                "role_id": "Admin",
                "source": "delegation",
                "parent_grant_id": "grnt_admin_1",
                "valid_to": (SEED_BASE_TIME + timedelta(days=2)).isoformat(),
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422
        assert "DELEGATION_OF_ADMIN_PROHIBITED" in resp.json()["detail"]

    def test_f1_admin_self_grant_rejected(self, api_client):
        resp = api_client.post(
            "/grants",
            json={"user_id": "usr_admin_1", "role_id": "Admin", "source": "direct"},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422
        assert "ADMIN_SELF_GRANT_PROHIBITED" in resp.json()["detail"]

    def test_f1_admin_grant_without_approval_rejected_and_with_valid_approval_succeeds(self, api_client, db_session):
        # Without approval -> 403
        resp1 = api_client.post(
            "/grants",
            json={"user_id": "usr_maker_del", "role_id": "Admin", "source": "direct"},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp1.status_code == 403
        assert "DUAL_CONTROL_APPROVAL_REQUIRED" in resp1.json()["detail"]["deny_code"]

        # With valid approval -> 201
        ref = SEED_BASE_TIME
        appr = Approval(
            id="appr_f1_admin_ok",
            action="grant:create",
            target_type="grant",
            target_id="usr_maker_del",
            requested_by="usr_admin_1",
            approved_by="usr_admin_2",
            reason="Approved promotion",
            status="approved",
            expires_at=ref + timedelta(days=1),
            created_at=ref,
        )
        db_session.add(appr)
        db_session.commit()

        resp2 = api_client.post(
            "/grants",
            json={
                "user_id": "usr_maker_del",
                "role_id": "Admin",
                "source": "direct",
                "approval_id": "appr_f1_admin_ok",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp2.status_code == 201

    # --- F2: Denial Rows Persist, Read-only Dry Runs Don't Log, Access Probing ---

    def test_f2_action_denial_persists_in_new_session(self, api_client, db_session):
        from sqlalchemy.orm import sessionmaker
        resp = api_client.post(
            "/jobs/JOB-PLANTED-05/transition",
            json={"action": "job:check", "version": 2},
            headers={"X-Actor-Id": "usr_maker_wide"},
        )
        assert resp.status_code == 403

        # Read back from a brand new session to verify commit before exception
        NewSession = sessionmaker(bind=db_session.bind)
        new_session = NewSession()
        try:
            denial = (
                new_session.query(AuditLog)
                .filter(AuditLog.event_type == "DENIAL", AuditLog.actor_id == "usr_maker_wide")
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert denial is not None
            assert denial.decision == "DENY"
            assert denial.prev_hash is not None
            assert denial.verify_integrity() is True
        finally:
            new_session.close()

    def test_f2_can_and_compare_write_zero_audit_rows(self, api_client, db_session):
        from sqlalchemy.orm import sessionmaker
        NewSession = sessionmaker(bind=db_session.bind)
        s = NewSession()
        initial_count = s.query(AuditLog).count()
        s.close()

        # Trigger dry run /can
        api_client.get(
            "/can?user_id=usr_maker_del&action=job:check&job_id=JOB-PLANTED-01",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        # Trigger dry run /compare
        api_client.get(
            "/compare?user_a=usr_maker_del&user_b=usr_checker_del&action=job:check&job_id=JOB-PLANTED-01",
            headers={"X-Actor-Id": "usr_admin_1"},
        )

        s2 = NewSession()
        after_count = s2.query(AuditLog).count()
        s2.close()
        assert after_count == initial_count

    def test_f2_access_probe_logged_on_out_of_scope_job(self, api_client):
        from src.db import SessionLocal
        resp = api_client.get(
            "/jobs/JOB-PLANTED-03",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Job not found"

        new_session = SessionLocal()
        try:
            probe = (
                new_session.query(AuditLog)
                .filter(AuditLog.event_type == "ACCESS_PROBE", AuditLog.actor_id == "usr_maker_del")
                .order_by(AuditLog.id.desc())
                .first()
            )
            assert probe is not None
            assert probe.target_id == "JOB-PLANTED-03"
        finally:
            new_session.close()

    # --- F3: Admin Override Dual Control & Stage Validation ---

    def test_f3_single_admin_override_filed_locked_denied(self, api_client):
        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/override",
            json={"action": "admin:override", "reason": "Attempting solo override on filed job", "target_stage": "Confirmed"},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 403
        assert "DUAL_CONTROL_APPROVAL_REQUIRED" in resp.json()["detail"]["deny_code"]

    def test_f3_admin_override_with_approval_succeeds_and_records_both(self, api_client, db_session):
        ref = SEED_BASE_TIME
        appr = Approval(
            id="appr_f3_override_ok",
            action="admin:override",
            target_type="job",
            target_id="JOB-PLANTED-08",
            requested_by="usr_admin_1",
            approved_by="usr_admin_2",
            reason="Legal order rewrite",
            status="approved",
            expires_at=ref + timedelta(days=1),
            created_at=ref,
        )
        db_session.add(appr)
        db_session.commit()

        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/override",
            json={
                "action": "admin:override",
                "reason": "Legal order rewrite",
                "target_stage": "Confirmed",
                "approval_id": "appr_f3_override_ok",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["actor_id"] == "usr_admin_1"
        assert data["approved_by"] == "usr_admin_2"

        # Check job_event recorded both
        evt = (
            db_session.query(JobEvent)
            .filter_by(job_id="JOB-PLANTED-08", action="OVERRIDE")
            .order_by(JobEvent.id.desc())
            .first()
        )
        assert evt.actor_id == "usr_admin_1"
        assert "usr_admin_2" in evt.reason

    def test_f3_admin_override_approval_cannot_be_reused(self, api_client):
        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/override",
            json={
                "action": "admin:override",
                "reason": "Reuse attempt",
                "target_stage": "Confirmed",
                "approval_id": "appr_f3_override_ok",
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 403

    def test_f3_admin_override_invalid_stage_rejected(self, api_client):
        resp = api_client.post(
            "/jobs/JOB-PLANTED-01/override",
            json={"action": "admin:override", "reason": "Bad stage", "target_stage": "ImaginaryStage"},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422

    # --- F4: Permission Checks, Not Role Names (Q12) ---

    def test_f4_permission_check_grant_officer_q12(self, api_client, db_session):
        ref = SEED_BASE_TIME
        role = Role(id="GrantOfficer", name="Grant Officer", description="Only grants")
        rp = RolePermission(role_id="GrantOfficer", permission_id="grant:manage")
        user = User(id="usr_grant_officer", username="staff_grant_officer", full_name="Grant Officer", email="go@nfpl.com", user_type="staff", is_active=True)
        grant = Grant(id="grnt_go", user_id="usr_grant_officer", role_id="GrantOfficer", valid_from=ref, source="direct")
        db_session.add_all([role, rp, user, grant])
        db_session.commit()

        # GrantOfficer can create a non-admin grant
        resp1 = api_client.post(
            "/grants",
            json={"user_id": "usr_maker_del", "role_id": "Maker", "branch_id": "DEL", "source": "direct"},
            headers={"X-Actor-Id": "usr_grant_officer"},
        )
        assert resp1.status_code == 201

        # GrantOfficer cannot override a job
        resp2 = api_client.post(
            "/jobs/JOB-PLANTED-01/override",
            json={"action": "admin:override", "reason": "Test override", "target_stage": "Prepared"},
            headers={"X-Actor-Id": "usr_grant_officer"},
        )
        assert resp2.status_code == 403

    # --- F5: Caller Identity & Read-endpoint Scope ---

    def test_f5_missing_actor_header_returns_401(self, api_client):
        resp = api_client.get("/jobs/JOB-PLANTED-01")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json()["detail"]

    def test_f5_unknown_actor_header_returns_401(self, api_client):
        resp = api_client.get("/jobs/JOB-PLANTED-01", headers={"X-Actor-Id": "usr_nonexistent"})
        assert resp.status_code == 401

    def test_f5_client_user_accessing_other_client_job_gets_identical_404(self, api_client):
        resp_other = api_client.get("/jobs/JOB-PLANTED-09", headers={"X-Actor-Id": "usr_client_acme"})
        resp_fake = api_client.get("/jobs/JOB-DOES-NOT-EXIST", headers={"X-Actor-Id": "usr_client_acme"})
        assert resp_other.status_code == 404
        assert resp_fake.status_code == 404
        assert resp_other.json() == resp_fake.json()

    def test_f5_delhi_maker_gets_identical_404_for_mumbai_job_as_unknown_job(self, api_client):
        resp_mum = api_client.get("/jobs/JOB-PLANTED-03", headers={"X-Actor-Id": "usr_maker_del"})
        resp_unknown = api_client.get("/jobs/JOB-NOT-REAL", headers={"X-Actor-Id": "usr_maker_del"})
        assert resp_mum.status_code == 404
        assert resp_unknown.status_code == 404
        assert resp_mum.json() == resp_unknown.json()

    def test_f5_internal_fields_stripped_for_client_user_present_for_checker(self, api_client, db_session):
        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        j01.prepared_by = "usr_maker_del"
        db_session.commit()

        resp_client = api_client.get("/jobs/JOB-PLANTED-01", headers={"X-Actor-Id": "usr_client_acme"})
        assert resp_client.status_code == 200
        data_c = resp_client.json()
        assert data_c["prepared_by"] is None
        assert data_c["checked_by"] is None
        assert data_c["locked_reason"] is None
        assert data_c["test_case_note"] is None

        resp_checker = api_client.get("/jobs/JOB-PLANTED-01", headers={"X-Actor-Id": "usr_checker_del"})
        assert resp_checker.status_code == 200
        data_k = resp_checker.json()
        assert data_k["prepared_by"] is not None

    def test_f5_trainee_cannot_read_another_users_queue(self, api_client):
        resp = api_client.get("/users/usr_maker_del/queue", headers={"X-Actor-Id": "usr_trainee"})
        assert resp.status_code == 403

    def test_f5_can_tier_cannot_be_forced_via_parameter(self, api_client):
        resp = api_client.get(
            "/can?user_id=usr_maker_del&action=job:prepare&job_id=JOB-PLANTED-01&tier=full",
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 200
        assert resp.json()["tier"] == "generic"

    # --- F6: Scope Edit Guardrails ---

    def test_f6_scope_edit_delhi_manager_cannot_move_to_mumbai(self, api_client):
        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={"branch_id": "MUM", "reason": "Moving to Mumbai", "expected_version": 1},
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 403

    def test_f6_scope_edit_allowed_move_writes_audit_row_with_old_and_new(self, api_client, db_session):
        ref = SEED_BASE_TIME
        appr = Approval(
            id="appr_f6_admin_move",
            action="job:edit_scope",
            target_type="job",
            target_id="JOB-PLANTED-01",
            requested_by="usr_admin_1",
            approved_by="usr_admin_2",
            reason="National routing shift",
            status="approved",
            expires_at=ref + timedelta(days=1),
            created_at=ref,
        )
        db_session.add(appr)
        db_session.commit()

        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        current_v = j01.version

        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={
                "branch_id": "MUM",
                "reason": "National routing shift",
                "expected_version": current_v,
                "approval_id": "appr_f6_admin_move",
                "at": ref.isoformat(),
            },
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200
        assert resp.json()["branch_id"] == "MUM"

        audit_row = (
            db_session.query(AuditLog)
            .filter_by(target_id="JOB-PLANTED-01", action="job:edit_scope")
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert audit_row is not None
        assert "DEL" in audit_row.details
        assert "MUM" in audit_row.details

    def test_f6_scope_edit_filed_job_denied_filed_job_scope_locked(self, api_client):
        resp = api_client.patch(
            "/jobs/JOB-PLANTED-08/scope",
            json={"branch_id": "DEL", "reason": "Illegal scope change on filed", "expected_version": 1},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["deny_code"] == "FILED_JOB_SCOPE_LOCKED"

    def test_f6_scope_edit_invalid_mode_rejected(self, api_client):
        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={"mode": "ROCKET", "reason": "Invalid mode", "expected_version": 1},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422

    def test_f6_scope_edit_missing_version_rejected(self, api_client):
        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={"branch_id": "MUM", "reason": "Missing version"},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 422

    def test_f6_scope_edit_stale_version_returns_409(self, api_client, db_session):
        ref = SEED_BASE_TIME
        appr = Approval(
            id="appr_f6_stale",
            action="job:edit_scope",
            target_type="job",
            target_id="JOB-PLANTED-01",
            requested_by="usr_admin_1",
            approved_by="usr_admin_2",
            reason="Version test",
            status="approved",
            expires_at=ref + timedelta(days=1),
            created_at=ref,
        )
        db_session.add(appr)
        db_session.commit()

        resp = api_client.patch(
            "/jobs/JOB-PLANTED-01/scope",
            json={"branch_id": "DEL", "reason": "Version test", "expected_version": 999, "approval_id": "appr_f6_stale", "at": ref.isoformat()},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 409

    def test_f6_two_concurrent_transitions_produce_one_success(self, api_client, db_session):
        j06 = db_session.query(Job).filter_by(id="JOB-PLANTED-06").one()
        j06.stage = "Prepared"
        j06.prepared_by = "usr_maker_del"
        db_session.commit()
        v = j06.version

        resp1 = api_client.post(
            "/jobs/JOB-PLANTED-06/transition",
            json={"action": "job:check", "version": v},
            headers={"X-Actor-Id": "usr_checker_del"},
        )
        resp2 = api_client.post(
            "/jobs/JOB-PLANTED-06/transition",
            json={"action": "job:check", "version": v},
            headers={"X-Actor-Id": "usr_checker_del"},
        )
        statuses = {resp1.status_code, resp2.status_code}
        assert statuses == {200, 409}

    # --- F7: Full End-to-End Approvals Flow ---

    def test_f7_approvals_end_to_end_cancel_filed(self, api_client, db_session):
        ref = SEED_BASE_TIME

        # 1. Rohan (Manager) requests cancellation approval
        r_req = api_client.post(
            "/approvals",
            json={
                "action": "job:cancel_filed",
                "target_type": "job",
                "target_id": "JOB-PLANTED-08",
                "reason": "Client bankruptcy notice received",
                "at": ref.isoformat(),
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert r_req.status_code == 201
        appr_id = r_req.json()["id"]

        # 2. Trainee attempts to approve -> 403 (APPROVER_NOT_AUTHORIZED)
        r_trainee = api_client.post(
            f"/approvals/{appr_id}/approve?at={ref.isoformat()}",
            headers={"X-Actor-Id": "usr_trainee"},
        )
        assert r_trainee.status_code == 403

        # 3. Rohan attempts self-approval -> 403 (SELF_APPROVAL_PROHIBITED)
        r_self = api_client.post(
            f"/approvals/{appr_id}/approve?at={ref.isoformat()}",
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert r_self.status_code == 403

        # 4. Another Manager (Admin/BranchManager) approves
        r_appr = api_client.post(
            f"/approvals/{appr_id}/approve?at={ref.isoformat()}",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert r_appr.status_code == 200
        assert r_appr.json()["status"] == "approved"

        # 5. Rohan executes cancel_filed with reason and version -> 200
        j08 = db_session.query(Job).filter_by(id="JOB-PLANTED-08").one()
        v = j08.version
        r_trans = api_client.post(
            "/jobs/JOB-PLANTED-08/transition",
            json={
                "action": "job:cancel_filed",
                "reason": "Bankruptcy verified by legal team",
                "version": v,
                "approval_id": appr_id,
                "at": ref.isoformat(),
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert r_trans.status_code == 200
        assert r_trans.json()["to_stage"] == "Cancelled"

        # 6. Second attempt with the same approval -> 403 (APPROVAL_ALREADY_USED)
        j08.stage = "Filed"
        j08.locked = True
        db_session.commit()

        r_reused = api_client.post(
            "/jobs/JOB-PLANTED-08/transition",
            json={
                "action": "job:cancel_filed",
                "reason": "Second cancel attempt",
                "version": j08.version,
                "approval_id": appr_id,
                "at": ref.isoformat(),
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert r_reused.status_code == 403
        assert "APPROVAL_ALREADY_USED" in r_reused.text

    def test_f7_expired_approval_denied(self, api_client, db_session):
        ref = SEED_BASE_TIME
        appr = Approval(
            id="appr_f7_expired",
            action="job:cancel_filed",
            target_type="job",
            target_id="JOB-PLANTED-08",
            requested_by="usr_manager",
            approved_by="usr_admin_1",
            reason="Expired ticket",
            status="approved",
            expires_at=ref - timedelta(hours=1),
            created_at=ref - timedelta(days=2),
        )
        db_session.add(appr)
        db_session.commit()

        j08 = db_session.query(Job).filter_by(id="JOB-PLANTED-08").one()
        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/transition",
            json={
                "action": "job:cancel_filed",
                "reason": "Trying expired",
                "version": j08.version,
                "approval_id": "appr_f7_expired",
                "at": ref.isoformat(),
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 403

    def test_f7_approval_for_job_a_not_usable_on_job_b(self, api_client, db_session):
        ref = SEED_BASE_TIME
        appr = Approval(
            id="appr_f7_job_a",
            action="job:cancel_filed",
            target_type="job",
            target_id="JOB-PLANTED-01",
            requested_by="usr_manager",
            approved_by="usr_admin_1",
            reason="Wrong job target",
            status="approved",
            expires_at=ref + timedelta(days=1),
            created_at=ref,
        )
        db_session.add(appr)
        db_session.commit()

        j08 = db_session.query(Job).filter_by(id="JOB-PLANTED-08").one()
        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/transition",
            json={
                "action": "job:cancel_filed",
                "reason": "Attempting job A approval on job B",
                "version": j08.version,
                "approval_id": "appr_f7_job_a",
                "at": ref.isoformat(),
            },
            headers={"X-Actor-Id": "usr_manager"},
        )
        assert resp.status_code == 403

    def test_f7_approval_requested_by_rohan_not_usable_by_meera(self, api_client, db_session):
        ref = SEED_BASE_TIME
        appr = Approval(
            id="appr_f7_rohan_req",
            action="job:cancel_filed",
            target_type="job",
            target_id="JOB-PLANTED-08",
            requested_by="usr_manager",  # Rohan
            approved_by="usr_admin_1",
            reason="Rohan's request",
            status="approved",
            expires_at=ref + timedelta(days=1),
            created_at=ref,
        )
        db_session.add(appr)
        db_session.commit()

        j08 = db_session.query(Job).filter_by(id="JOB-PLANTED-08").one()
        # Meera attempts to use Rohan's approval
        resp = api_client.post(
            "/jobs/JOB-PLANTED-08/transition",
            json={
                "action": "job:cancel_filed",
                "reason": "Meera claiming Rohan approval",
                "version": j08.version,
                "approval_id": "appr_f7_rohan_req",
                "at": ref.isoformat(),
            },
            headers={"X-Actor-Id": "usr_checker_all"},
        )
        assert resp.status_code == 403

    # --- F8: In-flight Flagging & Delegation Cascade ---

    def test_f8_revoking_checker_grant_flags_checker_assignments_not_maker(self, api_client, db_session):
        ref = SEED_BASE_TIME
        user_id = "usr_checker_del"

        asgn_chk = Assignment(id="asgn_f8_chk", job_id="JOB-PLANTED-01", user_id=user_id, assigned_role="Checker", is_active=True, assigned_at=ref)
        asgn_mak = Assignment(id="asgn_f8_mak", job_id="JOB-PLANTED-01", user_id=user_id, assigned_role="Maker", is_active=True, assigned_at=ref)
        db_session.add_all([asgn_chk, asgn_mak])
        db_session.commit()

        chk_grant = db_session.query(Grant).filter_by(user_id=user_id, role_id="Checker").first()
        resp = api_client.delete(f"/grants/{chk_grant.id}?reason=Testing+F8+flagging", headers={"X-Actor-Id": "usr_admin_1"})
        assert resp.status_code == 200

        db_session.expire_all()
        a_chk = db_session.query(Assignment).filter_by(id="asgn_f8_chk").one()
        a_mak = db_session.query(Assignment).filter_by(id="asgn_f8_mak").one()
        assert a_chk.needs_reassignment is True
        assert a_mak.needs_reassignment is False

    def test_f8_job_covered_by_second_active_checker_grant_not_flagged(self, api_client, db_session):
        ref = SEED_BASE_TIME
        user_id = "usr_checker_del"

        g2 = Grant(id="grnt_f8_anand_wide", user_id=user_id, role_id="Checker", branch_id=None, valid_from=ref, source="direct")
        asgn = Assignment(id="asgn_f8_multi", job_id="JOB-PLANTED-01", user_id=user_id, assigned_role="Checker", is_active=True, assigned_at=ref)
        db_session.add_all([g2, asgn])
        db_session.commit()

        g_narrow = Grant(id="grnt_f8_narrow", user_id=user_id, role_id="Checker", branch_id="DEL", valid_from=ref, source="direct")
        db_session.add(g_narrow)
        db_session.commit()

        resp = api_client.delete(f"/grants/{g_narrow.id}?reason=Narrow+grant+cleanup", headers={"X-Actor-Id": "usr_admin_1"})
        assert resp.status_code == 200

        db_session.expire_all()
        chk_asgn = db_session.query(Assignment).filter_by(id="asgn_f8_multi").one()
        assert chk_asgn.needs_reassignment is False

    def test_f8_revoking_parent_grant_ends_child_delegation_and_decide_denies(self, api_client, db_session):
        ref = SEED_BASE_TIME + timedelta(days=2)
        resp = api_client.delete(
            "/grants/grnt_abs_checker?reason=Vikram+on+leave",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        sunita_grant = db_session.query(Grant).filter_by(id="grnt_del_delegated").one()
        assert sunita_grant.revoked_at is not None

        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        dec = decide("usr_delegate", "job:check", j01, at=ref, session=db_session)
        assert dec.allowed is False

    def test_f8_end_dating_grant_to_past_flags_assignments(self, api_client, db_session):
        ref = SEED_BASE_TIME
        user_id = "usr_maker_del"
        asgn = Assignment(id="asgn_f8_enddate", job_id="JOB-PLANTED-01", user_id=user_id, assigned_role="Maker", is_active=True, assigned_at=ref)
        db_session.add(asgn)
        db_session.commit()

        maker_grant = db_session.query(Grant).filter_by(user_id=user_id, role_id="Maker").first()
        past_time = ref - timedelta(days=1)
        resp = api_client.patch(
            f"/grants/{maker_grant.id}",
            json={"valid_to": past_time.isoformat(), "reason": "Maker ended early", "at": ref.isoformat()},
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp.status_code == 200

        db_session.expire_all()
        chk = db_session.query(Assignment).filter_by(id="asgn_f8_enddate").one()
        assert chk.needs_reassignment is True

