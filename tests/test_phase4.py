"""
test_phase4.py — Test suite for Phase 4: Audit, History and Test Hardening.

Covers:
• GET /jobs/{id}/history: full timeline of events, with actors and reasons.
  Client users see stage changes only (internal notes, reasons, and staff names stripped).
• GET /jobs/{id}/who-could?on=DATE: replays end-dated grants to list everyone
  who could have changed the job on that date, with the grant used.
  Matches grants valid on past date including grants revoked afterwards.
• Hash-chained audit_log: each row stores hash of previous row.
  GET /audit/verify recomputes chain.
  Tampering with a historical row makes chain verification fail.
  Reading the log needs audit:read / audit:verify permission.
• No update or delete route exists for audit log (immutability).
• Denied attempts appear in log with deny codes.
• Report of repeated denials against filed jobs (early warning incident detection).
• Decision snapshots: logged action stores effective permissions at that moment.
"""

import json
from datetime import datetime, timedelta, timezone
import pytest
from fastapi.testclient import TestClient

from src.api.app import app, get_db
from src.models import (
    Job,
    JobEvent,
    AuditLog,
    Grant,
    User,
    Role,
    RolePermission,
)
from src.seeds.sample_data import SEED_BASE_TIME


@pytest.fixture
def api_client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


class TestJobHistoryAndPrivacy:
    """GET /jobs/{id}/history tests including client user privacy."""

    def test_internal_staff_sees_full_history_with_reasons_and_actors(self, api_client, db_session):
        """
        Staff (with job:view and job:view_internal) sees full event timeline
        with actor IDs, usernames, reasons, and state snapshots.
        """
        ref_time = SEED_BASE_TIME
        ev = JobEvent(
            id="ev_test_internal_01",
            job_id="JOB-PLANTED-01",
            actor_id="usr_maker_del",
            action="PREPARE",
            from_stage="Confirmed",
            to_stage="Prepared",
            reason="Verified packing list and bill of entry invoice #9921",
            old_values=json.dumps({"stage": "Confirmed"}),
            new_values=json.dumps({"stage": "Prepared"}),
            timestamp=ref_time,
        )
        db_session.add(ev)
        db_session.flush()

        resp = api_client.get(
            "/jobs/JOB-PLANTED-01/history",
            headers={"X-Actor-Id": "usr_checker_del"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "JOB-PLANTED-01"
        assert data["total_events"] >= 1

        matched = [e for e in data["events"] if e["id"] == "ev_test_internal_01"]
        assert len(matched) == 1
        item = matched[0]
        assert item["actor_id"] == "usr_maker_del"
        assert item["actor_name"] == "staff_maker_del"
        assert item["reason"] == "Verified packing list and bill of entry invoice #9921"
        assert item["old_values"] == {"stage": "Confirmed"}
        assert item["new_values"] == {"stage": "Prepared"}

    def test_client_user_sees_stage_changes_only_without_staff_names_or_notes(self, api_client, db_session):
        """
        Client user (usr_client_acme for CL-001) querying history sees stage transitions,
        but internal notes, staff names, reasons, and value snapshots are stripped.
        """
        ref_time = SEED_BASE_TIME
        ev = JobEvent(
            id="ev_test_client_privacy",
            job_id="JOB-PLANTED-01",  # CL-001
            actor_id="usr_maker_del",
            action="PREPARE",
            from_stage="Confirmed",
            to_stage="Prepared",
            reason="Confidential customs note: inspection waived by officer Sharma",
            old_values=json.dumps({"stage": "Confirmed", "internal_status": "ok"}),
            new_values=json.dumps({"stage": "Prepared", "internal_status": "done"}),
            timestamp=ref_time,
        )
        db_session.add(ev)
        db_session.flush()

        resp = api_client.get(
            "/jobs/JOB-PLANTED-01/history",
            headers={"X-Actor-Id": "usr_client_acme"},
        )
        assert resp.status_code == 200
        data = resp.json()

        matched = [e for e in data["events"] if e["id"] == "ev_test_client_privacy"]
        assert len(matched) == 1
        item = matched[0]
        assert item["from_stage"] == "Confirmed"
        assert item["to_stage"] == "Prepared"
        # Privacy guarantees:
        assert item["actor_id"] is None
        assert item["actor_name"] is None
        assert item["reason"] is None
        assert item["old_values"] is None
        assert item["new_values"] is None

    def test_client_user_accessing_different_client_job_history_returns_404(self, api_client):
        """
        Client user from Zenith (CL-002) attempting to read Acme's job history
        returns 404 with identical response to non-existent job.
        """
        resp = api_client.get(
            "/jobs/JOB-PLANTED-01/history",  # Acme job
            headers={"X-Actor-Id": "usr_client_zenith"},  # Zenith client
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Job not found"


class TestWhoCouldReplay:
    """GET /jobs/{id}/who-could?on=DATE tests."""

    def test_who_could_replays_past_date_including_revoked_grant(self, api_client, db_session):
        """
        A grant valid on past date T must appear in who-could candidates,
        even if the grant was revoked afterwards.
        """
        ref_time = SEED_BASE_TIME
        past_date = ref_time - timedelta(days=5)
        revocation_date = ref_time - timedelta(days=2)
        after_revocation = ref_time + timedelta(days=1)

        # Grant Maker role to usr_trainee on CL-001 DEL import air
        test_grant = Grant(
            id="grnt_test_who_could_revoked",
            user_id="usr_trainee",
            role_id="Maker",
            client_id="CL-001",
            branch_id="DEL",
            direction="IMPORT",
            mode="AIR",
            source="direct",
            valid_from=past_date - timedelta(days=2),
            valid_to=None,
            revoked_at=revocation_date,  # Revoked on day -2
            granted_by="usr_admin_1",
            created_at=past_date - timedelta(days=2),
        )
        db_session.add(test_grant)
        db_session.flush()

        # Query on past_date (day -5): grant was active!
        resp_past = api_client.get(
            f"/jobs/JOB-PLANTED-01/who-could?on={past_date.isoformat()}",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp_past.status_code == 200
        data_past = resp_past.json()
        candidate_ids = [c["user_id"] for c in data_past["candidates"]]
        assert "usr_trainee" in candidate_ids

        # Query after revocation (day +1): grant must NOT be in candidates!
        resp_after = api_client.get(
            f"/jobs/JOB-PLANTED-01/who-could?on={after_revocation.isoformat()}",
            headers={"X-Actor-Id": "usr_admin_1"},
        )
        assert resp_after.status_code == 200
        data_after = resp_after.json()
        candidate_ids_after = [c["user_id"] for c in data_after["candidates"]]
        assert "usr_trainee" not in candidate_ids_after

    def test_who_could_returns_specific_actions_and_grant_ids(self, api_client):
        """Candidates include the specific grant ID and allowed change actions."""
        ref_time = SEED_BASE_TIME
        resp = api_client.get(
            f"/jobs/JOB-PLANTED-01/who-could?on={ref_time.isoformat()}",
            headers={"X-Actor-Id": "usr_checker_del"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["job_id"] == "JOB-PLANTED-01"

        # Maker del should be able to perform job:prepare
        maker_candidates = [c for c in data["candidates"] if c["user_id"] == "usr_maker_del"]
        assert len(maker_candidates) >= 1
        assert "job:prepare" in maker_candidates[0]["actions"]

        # Checker del should be able to perform job:check
        checker_candidates = [c for c in data["candidates"] if c["user_id"] == "usr_checker_del"]
        assert len(checker_candidates) >= 1
        assert "job:check" in checker_candidates[0]["actions"]

    def test_who_could_client_user_forbidden(self, api_client):
        """Client users are prohibited from querying who-could audit replay."""
        resp = api_client.get(
            "/jobs/JOB-PLANTED-01/who-could",
            headers={"X-Actor-Id": "usr_client_acme"},
        )
        assert resp.status_code == 403


class TestAuditLogAndHashChain:
    """Audit log retrieval and cryptographic hash chain verification."""

    def test_audit_verify_clean_database(self, api_client):
        """Clean database audit chain verifies with 100% integrity."""
        resp = api_client.get(
            "/audit/verify",
            headers={"X-Actor-Id": "usr_auditor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["verified"] is True
        assert data["total_records"] >= 1
        assert data["head_hash"] is not None

    def test_tampering_with_historical_row_makes_chain_verification_fail(self, api_client, db_session):
        """
        Altering an audit row's action or decision in the database
        breaks the hash chain and is immediately flagged by GET /audit/verify.
        """
        # First verify it is valid
        r1 = api_client.get("/audit/verify", headers={"X-Actor-Id": "usr_auditor"})
        assert r1.status_code == 200
        assert r1.json()["verified"] is True

        # Tamper with an existing row
        target_log = db_session.query(AuditLog).filter(AuditLog.id == 2).first()
        if not target_log:
            target_log = db_session.query(AuditLog).first()
        orig_action = target_log.action
        target_log.action = "TAMPERED_ACTION_FORGED"
        db_session.flush()

        try:
            r2 = api_client.get("/audit/verify", headers={"X-Actor-Id": "usr_auditor"})
            assert r2.status_code == 200
            data2 = r2.json()
            assert data2["verified"] is False
            assert data2["broken_at_id"] == target_log.id
            assert "tampering detected" in data2["error"].lower()
        finally:
            # Restore the tampered row so other tests are not broken
            target_log.action = orig_action
            db_session.flush()

    def test_tampering_with_prev_hash_breaks_chain(self, api_client, db_session):
        """Modifying prev_hash also flags verification failure."""
        target_log = db_session.query(AuditLog).filter(AuditLog.id > 1).first()
        if target_log:
            orig_prev = target_log.prev_hash
            target_log.prev_hash = "f" * 64
            db_session.flush()

            try:
                resp = api_client.get("/audit/verify", headers={"X-Actor-Id": "usr_auditor"})
                assert resp.status_code == 200
                data = resp.json()
                assert data["verified"] is False
                assert data["broken_at_id"] == target_log.id
            finally:
                target_log.prev_hash = orig_prev
                db_session.flush()

    def test_audit_read_permission_enforcement(self, api_client):
        """Users without audit:read cannot list audit logs or verify the chain."""
        # usr_maker_del has Maker role without audit:read
        r_list = api_client.get("/audit", headers={"X-Actor-Id": "usr_maker_del"})
        assert r_list.status_code == 403

        r_verify = api_client.get("/audit/verify", headers={"X-Actor-Id": "usr_maker_del"})
        assert r_verify.status_code == 403

    def test_denied_attempts_appear_in_log_with_deny_codes(self, api_client, db_session):
        """
        Denied actions are recorded in the audit trail with decision='DENY'
        and explicit machine-readable deny codes.
        """
        j05 = db_session.query(Job).filter_by(id="JOB-PLANTED-05").one()
        # Trigger an intentional denial: Maker attempting to check own prepared job
        res = api_client.post(
            "/jobs/JOB-PLANTED-05/transition",
            json={"action": "job:check", "version": j05.version},
            headers={"X-Actor-Id": "usr_maker_wide"},
        )
        assert res.status_code == 403

        resp = api_client.get(
            "/audit?decision=DENY&target_id=JOB-PLANTED-05",
            headers={"X-Actor-Id": "usr_auditor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 1
        item = data["items"][-1]
        assert item["decision"] == "DENY"
        assert item["deny_code"] == "MAKER_CHECKER_CONFLICT"
        assert item["action"] == "job:check"
        assert item["actor_id"] == "usr_maker_wide"
        assert item["record_hash"] is not None

    def test_no_update_or_delete_route_exists_for_audit_log(self, api_client):
        """Immutability check: No PUT, PATCH, DELETE routes exist for /audit."""
        r_post = api_client.post("/audit", json={}, headers={"X-Actor-Id": "usr_admin_1"})
        assert r_post.status_code in (404, 405)

        r_put = api_client.put("/audit/1", json={}, headers={"X-Actor-Id": "usr_admin_1"})
        assert r_put.status_code in (404, 405)

        r_patch = api_client.patch("/audit/1", json={}, headers={"X-Actor-Id": "usr_admin_1"})
        assert r_patch.status_code in (404, 405)

        r_delete = api_client.delete("/audit/1", headers={"X-Actor-Id": "usr_admin_1"})
        assert r_delete.status_code in (404, 405)


class TestIncidentReportingAndSnapshots:
    """Incident early warnings and decision snapshots."""

    def test_repeated_filed_job_denials_report_generates_alerts(self, api_client, db_session):
        """
        Repeated denials on filed jobs are flagged in GET /audit/reports/filed-job-denials
        with CRITICAL / HIGH risk levels.
        """
        j08 = db_session.query(Job).filter_by(id="JOB-PLANTED-08").one()  # Filed job

        # Trigger 2 unapproved cancel attempts on filed job (simulating incident pattern)
        for _ in range(2):
            api_client.post(
                "/jobs/JOB-PLANTED-08/transition",
                json={
                    "action": "job:cancel_filed",
                    "reason": "Attempting unapproved cancel",
                    "version": j08.version,
                },
                headers={"X-Actor-Id": "usr_maker_del"},
            )

        resp = api_client.get(
            "/audit/reports/filed-job-denials",
            headers={"X-Actor-Id": "usr_auditor"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_filed_job_denials"] >= 2
        assert data["repeated_denial_incidents"] >= 1

        matched = [inc for inc in data["incidents"] if inc["job_id"] == "JOB-PLANTED-08"]
        assert len(matched) >= 1
        incident = matched[0]
        assert incident["is_filed_job"] is True
        assert incident["denial_count"] >= 2
        assert incident["risk_level"] in ("HIGH", "CRITICAL")
        assert any(code in incident["deny_codes"] for code in ("NO_PERMISSION", "APPROVAL_REQUIRED", "PERMISSION_DENIED"))

    def test_decision_snapshot_captured_on_stage_transition(self, api_client, db_session):
        """
        Successful stage transition persists actor's effective grants snapshot
        in AuditLog, ensuring history never depends on future grant state.
        """
        j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
        current_version = j01.version

        resp = api_client.post(
            "/jobs/JOB-PLANTED-01/transition",
            json={
                "action": "job:prepare",
                "version": current_version,
            },
            headers={"X-Actor-Id": "usr_maker_del"},
        )
        assert resp.status_code == 200

        # Query audit log for this transition
        r_audit = api_client.get(
            "/audit?event_type=STAGE_TRANSITION&target_id=JOB-PLANTED-01",
            headers={"X-Actor-Id": "usr_auditor"},
        )
        assert r_audit.status_code == 200
        items = r_audit.json()["items"]
        assert len(items) >= 1
        latest = items[-1]
        assert latest["decision"] == "EXECUTED"
        assert latest["permission_snapshot"] is not None
        assert latest["permission_snapshot"]["user_id"] == "usr_maker_del"
        assert len(latest["permission_snapshot"]["active_grants"]) >= 1
