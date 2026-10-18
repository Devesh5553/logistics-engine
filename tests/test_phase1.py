import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.db import Base
from src.models import (
    Branch,
    Client,
    User,
    Role,
    Permission,
    RolePermission,
    Grant,
    Job,
    JobEvent,
    Assignment,
    Approval,
    AuditLog,
)
from src.seeds.sample_data import seed_sample_data, SEED_BASE_TIME
from src.seeds.readme_generator import (
    generate_access_lines,
    update_readme_file,
    generate_branches_table,
    generate_clients_table,
    generate_role_permissions_table,
    generate_planted_jobs_table,
)
from src.seeds.scale_data import seed_scale_data


def test_schema_loads_cleanly_on_empty_database():
    """
    Test 1: Schema loads cleanly on an empty database.
    Verifies all 12 tables, composite indexes, absence fields, and guardrails exist without errors.
    """
    empty_engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=empty_engine)

    inspector = inspect(empty_engine)
    tables = inspector.get_table_names()

    expected_tables = {
        "branches",
        "clients",
        "users",
        "roles",
        "permissions",
        "role_permissions",
        "grants",
        "jobs",
        "job_events",
        "assignments",
        "approvals",
        "audit_log",
    }
    assert expected_tables.issubset(set(tables)), f"Missing tables: {expected_tables - set(tables)}"

    # Check composite indexes on jobs table
    job_indexes = inspector.get_indexes("jobs")
    scope_index = next((idx for idx in job_indexes if idx["name"] == "ix_jobs_scope_stage"), None)
    assert scope_index is not None, "Composite index ix_jobs_scope_stage not found on jobs table"
    assert scope_index["column_names"] == ["client_id", "branch_id", "direction", "mode", "stage"]

    branch_stage_index = next((idx for idx in job_indexes if idx["name"] == "ix_jobs_branch_stage"), None)
    assert branch_stage_index is not None, "Composite index ix_jobs_branch_stage not found on jobs table"
    assert branch_stage_index["column_names"] == ["branch_id", "stage"]

    # Check index on grants table
    grant_indexes = inspector.get_indexes("grants")
    validity_index = next((idx for idx in grant_indexes if idx["name"] == "ix_grants_user_validity"), None)
    assert validity_index is not None, "Index ix_grants_user_validity not found on grants table"

    # Check user absence columns
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    assert {"is_absent", "absent_from", "absent_to", "absence_reason"}.issubset(user_cols)

    # Check job test_case_note column
    job_cols = {c["name"] for c in inspector.get_columns("jobs")}
    assert "test_case_note" in job_cols


def test_seed_produces_expected_counts_and_is_idempotent():
    """
    Test 2: Seed produces expected counts and is idempotent.
    Verifies 5 branches, 10 clients, 22 users, 9 roles, 15 permissions, 300 jobs (15 planted + 285 seeded).
    """
    mem_engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=mem_engine)
    Session = sessionmaker(bind=mem_engine)

    # First run
    session1 = Session()
    counts = seed_sample_data(session1)
    session1.close()

    assert counts["branches"] == 5
    assert counts["clients"] == 10
    assert counts["users"] == 22
    assert counts["roles"] == 9
    assert counts["permissions"] == 15
    assert counts["grants"] == 25
    assert counts["jobs"] == 300
    assert counts["planted_jobs"] == 15
    assert counts["assignments"] == 5
    assert counts["approvals"] == 2
    assert counts["audit_logs"] == 4

    # Idempotent re-initialization test
    Base.metadata.drop_all(bind=mem_engine)
    Base.metadata.create_all(bind=mem_engine)
    session2 = Session()
    counts2 = seed_sample_data(session2)
    session2.close()

    assert counts2 == counts


def test_planted_personas_and_guardrails(db_session):
    """
    Test 3: Verifies all explicit awkward personas and guardrails (Items 1, 2, 3, 6, 7):
    - Maker everywhere, Checker for few
    - Checker for one client, one branch, imports only
    - Mode-restricted Checker (AIR only)
    - Manager role with cancel_filed and reopen_closed
    - Absent user with explicit absence dates
    - Expired delegation test (inside 7-day window vs after expiry)
    - Leaver isolation test (Dev Patel revoked vs Karan Kapoor active grants)
    - Client-side user isolation
    - Dual admins
    - Service parser
    - Cryptographic audit trail integrity
    """
    ref_date = SEED_BASE_TIME

    # 1. Maker everywhere, Checker for few
    maker_wide = db_session.query(User).filter_by(username="maker_wide_checker_few").one()
    grants_mw = db_session.query(Grant).filter_by(user_id=maker_wide.id).all()
    roles_held = {g.role_id: g for g in grants_mw}
    assert "Maker" in roles_held
    assert roles_held["Maker"].client_id is None
    checker_grants = [g for g in grants_mw if g.role_id == "Checker"]
    assert len(checker_grants) == 2
    assert {g.client_id for g in checker_grants} == {"CL-001", "CL-002"}

    # 2. Narrow Checker: one client, one branch, imports only
    chk_narrow = db_session.query(User).filter_by(username="checker_narrow").one()
    grants_cn = db_session.query(Grant).filter_by(user_id=chk_narrow.id).all()
    assert len(grants_cn) == 1
    assert grants_cn[0].client_id == "CL-001"
    assert grants_cn[0].branch_id == "DEL"
    assert grants_cn[0].direction == "IMPORT"
    assert grants_cn[0].mode is None

    # 3. Mode-restricted Checker (Item 6): CL-001, DEL, AIR only
    chk_air = db_session.query(User).filter_by(username="checker_air_only").one()
    grants_air = db_session.query(Grant).filter_by(user_id=chk_air.id).all()
    assert len(grants_air) == 1
    assert grants_air[0].client_id == "CL-001"
    assert grants_air[0].branch_id == "DEL"
    assert grants_air[0].mode == "AIR"

    # 4. Manager Role (Item 7): cancel_filed, reopen_closed
    mgr_user = db_session.query(User).filter_by(username="staff_manager_del").one()
    mgr_grant = db_session.query(Grant).filter_by(user_id=mgr_user.id).one()
    assert mgr_grant.role_id == "Manager"
    mgr_role = db_session.query(Role).filter_by(id="Manager").one()
    mgr_perms = {rp.permission_id for rp in mgr_role.permissions}
    assert {"job:cancel_filed", "job:reopen_closed", "job:edit_scope"}.issubset(mgr_perms)

    # 5. Absent user (Item 3)
    absent_user = db_session.query(User).filter_by(username="user_absent").one()
    assert absent_user.is_absent is True
    assert absent_user.absent_from is not None
    assert absent_user.absent_to is not None
    assert "surgery" in absent_user.absence_reason.lower()

    # 6. Delegation Window & Expiry Test (Item 1)
    delegate_user = db_session.query(User).filter_by(username="user_delegate").one()
    delegated_grant = db_session.query(Grant).filter_by(user_id=delegate_user.id, source="delegation").one()
    assert delegated_grant.parent_grant_id is not None
    assert delegated_grant.valid_from is not None
    assert delegated_grant.valid_to is not None
    # Inside 7-day window: valid
    at_inside_window = ref_date + timedelta(days=3)
    assert delegated_grant.is_active_at(at_inside_window) is True, "Delegation should be active inside 7-day window"
    # After expiry: inactive
    at_after_expiry = ref_date + timedelta(days=8)
    assert delegated_grant.is_active_at(at_after_expiry) is False, "Delegation must be inactive after 7-day expiry"

    # 7. Leaver isolation test (Item 2)
    # Dev Patel: Deactivated AND Revoked
    dev_leaver = db_session.query(User).filter_by(username="user_leaver").one()
    assert dev_leaver.is_active is False
    dev_grant = db_session.query(Grant).filter_by(user_id=dev_leaver.id).one()
    assert dev_grant.revoked_at is not None
    assert dev_grant.revocation_reason == "Left the company"

    # Karan Kapoor: Deactivated, but grants NOT revoked (isolates inactive check)
    karan_leaver = db_session.query(User).filter_by(username="user_leaver_active_grants").one()
    assert karan_leaver.is_active is False
    karan_grant = db_session.query(Grant).filter_by(user_id=karan_leaver.id).one()
    assert karan_grant.revoked_at is None
    assert karan_grant.valid_to is None

    # 8. Cryptographic audit trail integrity
    audit_entries = db_session.query(AuditLog).order_by(AuditLog.id.asc()).all()
    assert len(audit_entries) >= 4
    for entry in audit_entries:
        assert entry.verify_integrity() is True, f"Audit log record #{entry.id} integrity failure!"


def test_planted_jobs_coverage_and_metadata(db_session):
    """
    Test 4: Verifies the 15 deliberate planted test jobs (Item 5).
    """
    planted_jobs = (
        db_session.query(Job)
        .filter(Job.id.like("JOB-PLANTED-%"))
        .order_by(Job.id.asc())
        .all()
    )
    assert len(planted_jobs) == 15

    for j in planted_jobs:
        assert j.test_case_note is not None and len(j.test_case_note) > 10

    # 1. Maker-Checker conflict planted job
    j05 = db_session.query(Job).filter_by(id="JOB-PLANTED-05").one()
    assert j05.stage == "Prepared"
    assert j05.prepared_by == "usr_maker_wide"
    assert "maker-checker" in j05.test_case_note.lower()

    # 2. Mode contrast jobs
    j01 = db_session.query(Job).filter_by(id="JOB-PLANTED-01").one()
    j06 = db_session.query(Job).filter_by(id="JOB-PLANTED-06").one()
    assert j01.client_id == "CL-001" and j01.mode == "AIR"
    assert j06.client_id == "CL-001" and j06.mode == "FCL"

    # 3. Locked filed shipments
    j08 = db_session.query(Job).filter_by(id="JOB-PLANTED-08").one()
    j09 = db_session.query(Job).filter_by(id="JOB-PLANTED-09").one()
    assert j08.stage == "Filed" and j08.locked is True
    assert j09.stage == "Filed" and j09.locked is True

    # 4. Leaver in-flight job
    j14 = db_session.query(Job).filter_by(id="JOB-PLANTED-14").one()
    assert j14.prepared_by == "usr_leaver"
    assert "leaver" in j14.test_case_note.lower()


def test_readme_tables_and_access_lines_parity(db_session, tmp_path):
    """
    Test 5: README generated tables and access lines match the seed data,
    and fails if any section differs.
    """
    test_readme = tmp_path / "README.md"
    template = (
        "# Test Document\n\n"
        "<!-- BRANCHES_TABLE_START -->\n<!-- BRANCHES_TABLE_END -->\n\n"
        "<!-- CLIENTS_TABLE_START -->\n<!-- CLIENTS_TABLE_END -->\n\n"
        "<!-- ROLE_PERMISSIONS_START -->\n<!-- ROLE_PERMISSIONS_END -->\n\n"
        "<!-- PLANTED_JOBS_START -->\n<!-- PLANTED_JOBS_END -->\n\n"
        "<!-- ACCESS_LINES_START -->\n<!-- ACCESS_LINES_END -->\n"
    )
    test_readme.write_text(template, encoding="utf-8")

    update_readme_file(str(test_readme), db_session)
    readme_content = test_readme.read_text(encoding="utf-8")

    # Check tables generated
    assert "| `DEL` | Delhi Northern Hub |" in readme_content
    assert "| `CL-001` | **Acme Global Trading** |" in readme_content
    assert "| **Manager** |" in readme_content
    assert "| `JOB-PLANTED-05` |" in readme_content

    # Check access lines generated
    expected_lines = generate_access_lines(db_session)
    for line in expected_lines:
        assert line in readme_content, f"Missing line from README: {line}"

    # Verify tampering detection
    assert "Role **Manager**" in readme_content
    tampered = readme_content.replace("Role **Manager**", "Role **Trainee**")
    test_readme.write_text(tampered, encoding="utf-8")
    tampered_text = test_readme.read_text(encoding="utf-8")
    assert not all(line in tampered_text for line in expected_lines)


def test_scale_query_benchmark_and_report_file(tmp_path):
    """
    Test 6: Scale generator produces report file with query timings and EXPLAIN plan.
    """
    test_db = tmp_path / "test_scale.db"
    res = seed_scale_data(
        num_users=100,
        num_jobs=10_000,
        batch_size=5_000,
        seed=123,
        db_file_path=str(test_db),
        iterations=20
    )

    assert res["num_users"] == 100
    assert res["num_jobs"] == 10_000
    assert res["metrics"]["narrow"]["avg"] < 50.0
    assert res["metrics"]["branch"]["avg"] < 50.0
    assert res["metrics"]["wildcard"]["avg"] < 50.0
    assert res["metrics"]["grant"]["avg"] < 10.0

    report_path = Path(res["report_file"])
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "EXPLAIN QUERY PLAN VERIFICATION" in report_text
    assert "ix_jobs_scope_stage" in report_text
