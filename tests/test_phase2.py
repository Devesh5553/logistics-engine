import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from src.models import User, Job, Grant, AuditLog
from src.seeds.sample_data import SEED_BASE_TIME
from src.engine import decide, compare_access
from src.api.app import app, get_db


@pytest.fixture
def api_client(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


def test_wildcard_maker_with_narrow_checker_grants(db_session):
    """
    Q2: Rohan (maker_wide_checker_few) has Maker everywhere, Checker for CL-001 & CL-002 only.
    """
    ref_date = SEED_BASE_TIME
    rohan = db_session.query(User).filter_by(username="maker_wide_checker_few").one()

    # Can prepare on CL-001 (Delhi, Import, Air)
    res_prep_cl1 = decide(rohan, "job:prepare", "JOB-PLANTED-01", at=ref_date, session=db_session)
    assert res_prep_cl1.allowed is True
    assert "POLICY_ALLOW" in res_prep_cl1.rule

    # Can prepare on CL-003 (Delhi, Import, Air) - Maker is wildcard across all clients
    res_prep_cl3 = decide(rohan, "job:prepare", "JOB-PLANTED-04", at=ref_date, session=db_session)
    assert res_prep_cl3.allowed is True

    # Can check on CL-001 when he is NOT the preparer (JOB-PLANTED-06 prepared by staff_maker_del)
    # Put JOB-PLANTED-06 in Prepared stage prepared by someone else
    j06 = db_session.query(Job).filter_by(id="JOB-PLANTED-06").one()
    j06.stage = "Prepared"
    j06.prepared_by = "usr_maker_del"
    db_session.flush()

    res_check_cl1 = decide(rohan, "job:check", j06, at=ref_date, session=db_session)
    assert res_check_cl1.allowed is True

    # Cannot check on CL-003 (outside his checker scope!)
    j04 = db_session.query(Job).filter_by(id="JOB-PLANTED-04").one()
    j04.stage = "Prepared"
    j04.prepared_by = "usr_maker_del"
    db_session.flush()

    res_check_cl3 = decide(rohan, "job:check", j04, at=ref_date, session=db_session)
    assert res_check_cl3.allowed is False
    assert res_check_cl3.deny_code in ["NO_PERMISSION", "OUT_OF_SCOPE"]


def test_narrow_checker_slice_enforcement(db_session):
    """
    Q2: Kavita (checker_narrow) is restricted to CL-001, DEL, IMPORT.
    """
    ref_date = SEED_BASE_TIME
    kavita = db_session.query(User).filter_by(username="checker_narrow").one()

    # Allowed inside her slice: CL-001, DEL, IMPORT, AIR
    j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
    j01.stage = "Prepared"
    j01.prepared_by = "usr_maker_del"
    db_session.flush()

    res_in_slice = decide(kavita, "job:check", j01, at=ref_date, session=db_session)
    assert res_in_slice.allowed is True

    # Denied on EXPORT (JOB-PLANTED-02: CL-001, DEL, EXPORT)
    j02 = db_session.query(Job).filter_by(id="JOB-PLANTED-02").one()
    j02.stage = "Prepared"
    j02.prepared_by = "usr_maker_del"
    db_session.flush()

    res_export = decide(kavita, "job:check", j02, at=ref_date, session=db_session)
    assert res_export.allowed is False
    assert res_export.deny_code == "OUT_OF_SCOPE"

    # Denied on another branch (JOB-PLANTED-03: CL-001, MUM, IMPORT)
    j03 = db_session.query(Job).filter_by(id="JOB-PLANTED-03").one()
    j03.stage = "Prepared"
    j03.prepared_by = "usr_maker_mum"
    db_session.flush()

    res_mumbai = decide(kavita, "job:check", j03, at=ref_date, session=db_session)
    assert res_mumbai.allowed is False
    assert res_mumbai.deny_code == "OUT_OF_SCOPE"


def test_mode_restricted_checker(db_session):
    """
    Item 6: Sanjay (checker_air_only) is restricted to CL-001, DEL, AIR.
    """
    ref_date = SEED_BASE_TIME
    sanjay = db_session.query(User).filter_by(username="checker_air_only").one()

    # Allowed on AIR (JOB-PLANTED-01)
    j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
    j01.stage = "Prepared"
    j01.prepared_by = "usr_maker_del"
    db_session.flush()

    res_air = decide(sanjay, "job:check", j01, at=ref_date, session=db_session)
    assert res_air.allowed is True

    # Denied on FCL ocean container (JOB-PLANTED-06)
    j06 = db_session.query(Job).filter_by(id="JOB-PLANTED-06").one()
    j06.stage = "Prepared"
    j06.prepared_by = "usr_maker_del"
    db_session.flush()

    res_fcl = decide(sanjay, "job:check", j06, at=ref_date, session=db_session)
    assert res_fcl.allowed is False
    assert res_fcl.deny_code == "OUT_OF_SCOPE"


def test_maker_checker_conflict_separation_of_duties(db_session):
    """
    Q3: Rohan prepared JOB-PLANTED-05. He holds an active Checker grant for CL-001,
    but MUST be denied the check because he prepared the checklist.
    """
    ref_date = SEED_BASE_TIME
    rohan = db_session.query(User).filter_by(username="maker_wide_checker_few").one()
    kavita = db_session.query(User).filter_by(username="checker_narrow").one()
    j05 = db_session.query(Job).filter_by(id="JOB-PLANTED-05").one()

    assert j05.prepared_by == rohan.id
    assert j05.stage == "Prepared"

    # Rohan attempts check on his own preparation: DENIED
    res_rohan = decide(rohan, "job:check", j05, at=ref_date, session=db_session)
    assert res_rohan.allowed is False
    assert res_rohan.deny_code == "MAKER_CHECKER_CONFLICT"
    assert "separation of duties" in res_rohan.reasons[0].lower()

    # Another qualified checker (Kavita) checks the same job: ALLOWED
    res_kavita = decide(kavita, "job:check", j05, at=ref_date, session=db_session)
    assert res_kavita.allowed is True


def test_client_user_isolation_and_obfuscation(db_session):
    """
    Q7 & Q19: Client user Acme (John Doe) can only view CL-001 jobs.
    Accessing another client's job (Zenith CL-002) returns NOT_FOUND (404),
    matching the response for a non-existent job.
    """
    ref_date = SEED_BASE_TIME
    john_acme = db_session.query(User).filter_by(username="client_acme_user").one()

    # View own job (JOB-PLANTED-01: CL-001): ALLOWED
    res_own = decide(john_acme, "job:view", "JOB-PLANTED-01", at=ref_date, session=db_session)
    assert res_own.allowed is True

    # Cannot act on own job (read-only): DENIED
    res_act = decide(john_acme, "job:prepare", "JOB-PLANTED-01", at=ref_date, session=db_session)
    assert res_act.allowed is False
    assert res_act.deny_code == "CLIENT_USER_READ_ONLY"

    # Access Zenith's job (JOB-PLANTED-09: CL-002): returns NOT_FOUND
    res_other = decide(john_acme, "job:view", "JOB-PLANTED-09", at=ref_date, session=db_session)
    assert res_other.allowed is False
    assert res_other.deny_code == "NOT_FOUND"

    # Access non-existent job: returns identical NOT_FOUND
    res_missing = decide(john_acme, "job:view", "JOB-DOES-NOT-EXIST", at=ref_date, session=db_session)
    assert res_missing.allowed is False
    assert res_missing.deny_code == "NOT_FOUND"


def test_inactive_users_and_grant_expiry(db_session):
    """
    Q16 & Item 1, Item 2:
    - Dev Patel (leaver, deactivated + revoked): DENIED (USER_INACTIVE)
    - Karan Kapoor (deactivated, unrevoked grants): DENIED (USER_INACTIVE) - isolates inactive check!
    - Anand Verma (revoked grant): DENIED (OUT_OF_SCOPE)
    - Sunita Verma (7-day delegation): active at T+3, expired at T+8
    """
    ref_date = SEED_BASE_TIME
    j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()

    # 1. Dev Patel (deactivated)
    dev = db_session.query(User).filter_by(username="user_leaver").one()
    res_dev = decide(dev, "job:prepare", j01, at=ref_date, session=db_session)
    assert res_dev.allowed is False
    assert res_dev.deny_code == "USER_INACTIVE"

    # 2. Karan Kapoor (deactivated, but grants still active - isolates status check)
    karan = db_session.query(User).filter_by(username="user_leaver_active_grants").one()
    res_karan = decide(karan, "job:prepare", j01, at=ref_date, session=db_session)
    assert res_karan.allowed is False
    assert res_karan.deny_code == "USER_INACTIVE"

    # 3. Anand Verma (active user, revoked grant)
    anand = db_session.query(User).filter_by(username="user_revoked").one()
    j01.stage = "Prepared"
    db_session.flush()
    res_anand = decide(anand, "job:check", j01, at=ref_date, session=db_session)
    assert res_anand.allowed is False
    assert res_anand.deny_code == "OUT_OF_SCOPE"

    # 4. Sunita Verma delegation window
    sunita = db_session.query(User).filter_by(username="user_delegate").one()
    # At T+3 days: active inside window
    res_sunita_valid = decide(sunita, "job:check", j01, at=ref_date + timedelta(days=3), session=db_session)
    assert res_sunita_valid.allowed is True
    # At T+8 days: expired
    res_sunita_exp = decide(sunita, "job:check", j01, at=ref_date + timedelta(days=8), session=db_session)
    assert res_sunita_exp.allowed is False
    assert res_sunita_exp.deny_code == "OUT_OF_SCOPE"


def test_parser_stage_restrictions(db_session):
    """
    Q9: Parser account can create and prepare at Confirmed and Prepared,
    never file, cancel, reopen or touch locked/filed jobs.
    """
    ref_date = SEED_BASE_TIME
    parser = db_session.query(User).filter_by(username="service_parser").one()

    # Allowed at Confirmed (JOB-PLANTED-01)
    res_conf = decide(parser, "job:prepare", "JOB-PLANTED-01", at=ref_date, session=db_session)
    assert res_conf.allowed is True

    # Denied filing (JOB-PLANTED-07 in Checked)
    res_file = decide(parser, "job:file", "JOB-PLANTED-07", at=ref_date, session=db_session)
    assert res_file.allowed is False
    assert res_file.deny_code in ["NO_PERMISSION", "PARSER_STAGE_RESTRICTION"]

    # Denied touching locked Filed job (JOB-PLANTED-08)
    res_filed = decide(parser, "job:prepare", "JOB-PLANTED-08", at=ref_date, session=db_session)
    assert res_filed.allowed is False
    assert res_filed.deny_code == "PARSER_STAGE_RESTRICTION"


def test_fail_closed_safeguard(db_session):
    """
    Q14: Fail-closed behavior on forced errors or missing data.
    """
    # Passing None session with string IDs triggers fail-closed error handling
    res = decide("usr_maker_wide", "job:prepare", "JOB-PLANTED-01", session=None)
    assert res.allowed is False
    assert res.deny_code == "FAIL_CLOSED_ERROR"


def test_explanation_tiers(db_session):
    """
    Two explanation tiers: full detail for Admin/Auditor, generic wording for staff.
    """
    ref_date = SEED_BASE_TIME
    admin = db_session.query(User).filter_by(username="admin_primary").one()
    staff = db_session.query(User).filter_by(username="staff_maker_del").one()

    # Full tier requested by Admin
    res_admin = decide(staff, "job:prepare", "JOB-PLANTED-01", at=ref_date, session=db_session, requester_or_id=admin)
    assert res_admin.tier == "full"
    assert len(res_admin.grant_ids) > 0
    assert "grant(s)" in res_admin.reasons[0].lower()

    # Generic tier requested by regular staff
    res_staff = decide(staff, "job:prepare", "JOB-PLANTED-01", at=ref_date, session=db_session, requester_or_id=staff)
    assert res_staff.tier == "generic"
    assert res_staff.reasons[0] == "Action is permitted."


def test_compare_access_and_differential_summary(db_session):
    """
    Q10: "Why can she do this and he can't?"
    Compares Kavita (allowed check) vs Rohan (denied check due to Maker-Checker conflict) on JOB-PLANTED-05.
    """
    ref_date = SEED_BASE_TIME
    comp = compare_access(
        user_a_or_id="usr_checker_del",
        user_b_or_id="usr_maker_wide",
        action="job:check",
        job_or_id="JOB-PLANTED-05",
        at=ref_date,
        session=db_session,
    )

    assert comp.user_a.allowed is True
    assert comp.user_b.allowed is False
    assert comp.user_b.deny_code == "MAKER_CHECKER_CONFLICT"
    assert "Maker-Checker conflict" in comp.differential_summary or "prepared" in comp.differential_summary


def test_denied_attempt_audit_logging_hook(db_session):
    """
    Q18: Denied attempts are logged to audit_log with tamper-evident hash chaining.
    """
    ref_date = SEED_BASE_TIME
    kavita = db_session.query(User).filter_by(username="checker_narrow").one()
    j02 = db_session.query(Job).filter_by(id="JOB-PLANTED-02").one()  # EXPORT job (Kavita is Import only)

    initial_audit_count = db_session.query(AuditLog).count()

    # Trigger out-of-scope denial
    res = decide(kavita, "job:check", j02, at=ref_date, session=db_session, log_denials=True)
    assert res.allowed is False

    new_audit_count = db_session.query(AuditLog).count()
    assert new_audit_count == initial_audit_count + 1

    latest_log = db_session.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert latest_log.event_type == "DENIAL"
    assert latest_log.decision == "DENY"
    assert latest_log.deny_code == "OUT_OF_SCOPE"
    assert latest_log.verify_integrity() is True


def test_api_endpoints_via_testclient(api_client, db_session):
    """
    API Integration Tests:
    - GET /can returns 200 with DecisionResult
    - GET /can for unauthorized client user returns 404
    - GET /compare returns 200 with ComparisonResult
    """
    # 1. /can allowed
    resp1 = api_client.get(
        "/can?user_id=usr_maker_wide&action=job:prepare&job_id=JOB-PLANTED-01",
        headers={"X-Actor-Id": "usr_admin_1"},
    )
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["allowed"] is True

    # 2. /can client user accessing other client job returns 404 (obfuscation)
    resp2 = api_client.get(
        "/can?user_id=usr_client_acme&action=job:view&job_id=JOB-PLANTED-09",
        headers={"X-Actor-Id": "usr_client_acme"},
    )
    assert resp2.status_code == 404
    assert resp2.json()["detail"] == "Job not found"

    # 3. /compare
    resp3 = api_client.get(
        "/compare?user_a=usr_checker_del&user_b=usr_maker_wide&action=job:check&job_id=JOB-PLANTED-05",
        headers={"X-Actor-Id": "usr_admin_1"},
    )
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["user_a"]["allowed"] is True
    assert data3["user_b"]["allowed"] is False
    assert len(data3["differential_summary"]) > 20
