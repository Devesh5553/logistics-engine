import json
import random
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session

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

# Deterministic base timestamp for sample data
SEED_BASE_TIME = datetime(2026, 1, 15, 9, 0, 0, tzinfo=timezone.utc)


def seed_sample_data(session: Session, ref_date: datetime = SEED_BASE_TIME) -> dict:
    """
    Populates the small, deliberate dataset:
    - 5 branches
    - 10 clients
    - 22 users with explicit planted personas (dual admins, manager, mode-restricted, dual leavers, absent & delegate)
    - 300 jobs (15 fixed planted test jobs + 285 seeded operational jobs across all stages)
    - Assignments, approvals, and hash-chained audit log entries
    """
    now = ref_date

    # 1. Branches (5 branches: standard 3-letter IATA / hub codes)
    branches = [
        Branch(id="DEL", name="Delhi Northern Hub", city="New Delhi", created_at=now),
        Branch(id="MUM", name="Mumbai Port & Air Cargo", city="Mumbai", created_at=now),
        Branch(id="KOL", name="Kolkata Eastern Port", city="Kolkata", created_at=now),
        Branch(id="MAA", name="Chennai Southern Hub", city="Chennai", created_at=now),
        Branch(id="BLR", name="Bengaluru Tech Depot", city="Bengaluru", created_at=now),
    ]
    session.add_all(branches)
    session.flush()

    # 2. Clients (10 clients)
    clients_data = [
        ("CL-001", "Acme Global Trading", "Consumer Goods"),
        ("CL-002", "Zenith Electronics", "High-Tech"),
        ("CL-003", "Bharat Auto Components", "Automotive"),
        ("CL-004", "Himalaya Pharmaceuticals", "Healthcare"),
        ("CL-005", "Orient Textiles", "Apparel"),
        ("CL-006", "Pacific Solar Energy", "Renewables"),
        ("CL-007", "Indus Chemicals", "Specialty Chemicals"),
        ("CL-008", "Deccan Heavy Machinery", "Industrial"),
        ("CL-009", "Royal Gems & Jewellery", "Luxury"),
        ("CL-010", "Sunrise Agro Exports", "Agriculture"),
    ]
    clients = [
        Client(id=cid, name=cname, industry=cind, is_active=True, created_at=now)
        for cid, cname, cind in clients_data
    ]
    session.add_all(clients)
    session.flush()

    # 3. Permissions (View separated from Act - Q1, with guardrail metadata - Items 4 & 7)
    permissions_data = [
        # (id, name, category, description, requires_reason, requires_second_approver, stage_limits)
        ("job:view", "View Job", "view", "Read basic job attributes and progress", False, False, "All stages"),
        ("job:view_internal", "View Internal Details", "view", "Read staff assignments, internal notes", False, False, "All stages"),
        ("job:create", "Create Job", "act", "Initiate new shipment job", False, False, "Confirmed"),
        ("job:prepare", "Prepare Job", "act", "Compile customs checklist and advance to Prepared", False, False, "Confirmed -> Prepared"),
        ("job:check", "Check Job", "act", "Verify customs checklist and advance to Checked", False, False, "Prepared -> Checked"),
        ("job:file", "File Job", "act", "Final customs filing advancing to Filed", False, False, "Checked -> Filed"),
        ("job:cancel_filed", "Cancel Filed Job", "act", "Cancel already filed shipment (High Risk)", True, True, "Filed"),
        ("job:reopen_closed", "Reopen Closed Job", "act", "Reopen filed or cancelled job (High Risk)", True, True, "Filed, Cancelled"),
        ("job:edit_scope", "Edit Scope", "act", "Change branch, client, direction, or mode (High Risk)", True, True, "Confirmed, Prepared, Checked, Reopened"),
        ("job:assign", "Assign Job", "act", "Assign job to staff members", False, False, "Confirmed, Prepared, Checked, Reopened"),
        ("grant:manage", "Manage Grants", "admin", "Create, end-date, or revoke user grants", True, False, "N/A"),
        ("user:manage", "Manage Users", "admin", "Manage user profiles and activation", True, False, "N/A"),
        ("audit:read", "Read Audit Logs", "admin", "View append-only audit trail", False, False, "N/A"),
        ("audit:verify", "Verify Audit Chain", "admin", "Verify SHA-256 integrity of audit log", False, False, "N/A"),
        ("admin:override", "Admin Override", "admin", "Emergency unstick of locked/stranded jobs", True, False, "All stages"),
    ]
    permissions = [
        Permission(
            id=pid, name=pname, category=pcat, description=pdesc,
            requires_reason=req_r, requires_second_approver=req_a, stage_limits=stg_lim
        )
        for pid, pname, pcat, pdesc, req_r, req_a, stg_lim in permissions_data
    ]
    session.add_all(permissions)
    session.flush()

    # 4. Roles & Role-Permissions (Q1, Q12, Item 4 & Item 7)
    roles_def = {
        "Maker": ["job:view", "job:view_internal", "job:create", "job:prepare"],
        "Checker": ["job:view", "job:view_internal", "job:check"],
        "Filer": ["job:view", "job:view_internal", "job:file"],
        "Manager": [
            "job:view", "job:view_internal", "job:cancel_filed", "job:reopen_closed",
            "job:edit_scope", "job:assign"
        ],
        "Admin": [
            "job:view", "job:view_internal", "grant:manage", "user:manage",
            "audit:read", "audit:verify", "admin:override", "job:assign", "job:edit_scope"
        ],
        "Auditor": ["job:view", "job:view_internal", "audit:read", "audit:verify"],
        "Viewer": ["job:view", "job:view_internal"],
        "ClientUser": ["job:view"],  # Strictly client-facing view, internal notes hidden
        "Parser": ["job:view", "job:create", "job:prepare"],  # Limited to Confirmed and Prepared
    }

    role_descriptions = {
        "Maker": "Prepares customs entries and shipping bills from Confirmed stage",
        "Checker": "Verifies prepared customs documentation (Separation of duties applies)",
        "Filer": "Submits verified declarations to customs portal ICEGATE",
        "Manager": "High-risk authority: dual-control cancellation, reopening, scope changes",
        "Admin": "System configuration, user provisioning, and audit oversight",
        "Auditor": "Read-only inspection of operations and cryptographic audit verification",
        "Viewer": "General internal visibility without operational execution rights",
        "ClientUser": "External customer tracking: isolated strictly to customer's own jobs",
        "Parser": "Automated EDI parser: limited to Confirmed and Prepared stages only",
    }

    for r_id, p_ids in roles_def.items():
        role = Role(
            id=r_id,
            name=r_id,
            description=role_descriptions.get(r_id, f"Standard {r_id} role"),
            is_system=True,
            created_at=now,
        )
        session.add(role)
        session.flush()
        for pid in p_ids:
            rp = RolePermission(role_id=r_id, permission_id=pid)
            session.add(rp)
    session.flush()

    # 5. Users (22 Users with explicit planted personas)
    # Item 1 (7-day delegation), Item 2 (dual leavers), Item 3 (absence window),
    # Item 6 (mode-restricted), Item 7 (manager role)
    users_data = [
        # (id, username, full_name, user_type, client_id, is_active, deactivated_at, is_absent, absent_from, absent_to, absence_reason)
        ("usr_admin_1", "admin_primary", "Aarav Sharma (Head of Compliance)", "staff", None, True, None, False, None, None, None),
        ("usr_admin_2", "admin_secondary", "Priya Nair (Operations Director)", "staff", None, True, None, False, None, None, None),
        ("usr_manager", "staff_manager_del", "Vikrant Deshmukh (Branch Operations Manager)", "staff", None, True, None, False, None, None, None),
        ("usr_maker_wide", "maker_wide_checker_few", "Rohan Mehta (Senior Maker / Occasional Checker)", "staff", None, True, None, False, None, None, None),
        ("usr_checker_del", "checker_narrow", "Kavita Rao (Delhi Import Checker Specialist)", "staff", None, True, None, False, None, None, None),
        ("usr_checker_air", "checker_air_only", "Sanjay Singhania (Air Freight Specialist)", "staff", None, True, None, False, None, None, None),
        ("usr_absent", "user_absent", "Vikram Malhotra (On Medical Leave)", "staff", None, True, None, True, now - timedelta(days=2), now + timedelta(days=7), "Medical leave following surgery"),
        ("usr_delegate", "user_delegate", "Sunita Verma (Designated Cover for Vikram)", "staff", None, True, None, False, None, None, None),
        ("usr_revoked", "user_revoked", "Anand Verma (Transferred to Accounts)", "staff", None, True, None, False, None, None, None),
        ("usr_leaver", "user_leaver", "Dev Patel (Former Operations Officer)", "staff", None, False, now - timedelta(days=10), False, None, None, None),
        ("usr_leaver_active", "user_leaver_active_grants", "Karan Kapoor (Deactivated Staff with Unrevoked Grants)", "staff", None, False, now - timedelta(days=5), False, None, None, None),
        ("usr_client_acme", "client_acme_user", "John Doe (Acme Logistics Manager)", "client_user", "CL-001", True, None, False, None, None, None),
        ("usr_client_zenith", "client_zenith_user", "Li Wei (Zenith Supply Chain Lead)", "client_user", "CL-002", True, None, False, None, None, None),
        ("usr_parser", "service_parser", "AutoEDIPayloadParser (Nightly Cron Job)", "service_account", None, True, None, False, None, None, None),
        ("usr_maker_del", "staff_maker_del", "Amit Kumar (Delhi Customs Preparer)", "staff", None, True, None, False, None, None, None),
        ("usr_maker_mum", "staff_maker_mum", "Neha Joshi (Mumbai Port Specialist)", "staff", None, True, None, False, None, None, None),
        ("usr_checker_mum", "staff_checker_mum", "Rajesh Pillai (Mumbai Lead Checker)", "staff", None, True, None, False, None, None, None),
        ("usr_maker_all", "staff_maker_all", "Deepak Chopra (National Maker)", "staff", None, True, None, False, None, None, None),
        ("usr_checker_all", "staff_checker_all", "Meera Iyer (National Senior Checker)", "staff", None, True, None, False, None, None, None),
        ("usr_filer_all", "staff_filer_all", "Suresh Menon (Authorized Customs Broker Filer)", "staff", None, True, None, False, None, None, None),
        ("usr_auditor", "staff_auditor", "Divya Sen (Internal Risk & Audit Officer)", "staff", None, True, None, False, None, None, None),
        ("usr_trainee", "staff_trainee", "Pooja Gupta (Junior Trainee)", "staff", None, True, None, False, None, None, None),
    ]

    users = []
    for uid, uname, fname, utype, cid, active, deact, is_abs, abs_f, abs_t, abs_r in users_data:
        u = User(
            id=uid,
            username=uname,
            full_name=fname,
            email=f"{uname}@nagarkot.com" if utype != "client_user" else f"{uname}@client.com",
            user_type=utype,
            client_id=cid,
            is_active=active,
            deactivated_at=deact,
            is_absent=is_abs,
            absent_from=abs_f,
            absent_to=abs_t,
            absence_reason=abs_r,
            created_at=now - timedelta(days=60),
        )
        users.append(u)
    session.add_all(users)
    session.flush()

    # 6. Grants (Scoped RBAC)
    # Rules: Never overwritten, only end-dated. NULL attribute = wildcard/all.
    grants_data = [
        # Dual Admins: Admin role across all clients, branches, directions, modes
        ("grnt_admin_1", "usr_admin_1", "Admin", None, None, None, None, "usr_admin_2", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_admin_2", "usr_admin_2", "Admin", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Manager: High-risk permissions (cancel filed, reopen closed, scope edit) in DEL
        ("grnt_manager_del", "usr_manager", "Manager", None, "DEL", None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Planted 1: Maker for every client, Checker for only CL-001 and CL-002
        ("grnt_mwcf_maker", "usr_maker_wide", "Maker", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_mwcf_check1", "usr_maker_wide", "Checker", "CL-001", None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_mwcf_check2", "usr_maker_wide", "Checker", "CL-002", None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Planted 2: Checker for one client (CL-001), one branch (DEL), imports only
        ("grnt_cn_check", "usr_checker_del", "Checker", "CL-001", "DEL", "IMPORT", None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Planted 6 (Item 6): Mode-restricted Checker (CL-001, DEL, AIR only)
        ("grnt_air_check", "usr_checker_air", "Checker", "CL-001", "DEL", None, "AIR", "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Planted 3: Absent user (Maker & Checker in DEL)
        ("grnt_abs_maker", "usr_absent", "Maker", None, "DEL", None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_abs_checker", "usr_absent", "Checker", None, "DEL", None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Planted 4 (Item 1): Sunita Verma delegation from Vikram (7 days window from seeding)
        (
            "grnt_del_delegated", "usr_delegate", "Checker", None, "DEL", None, None, "usr_admin_1",
            "delegation", "grnt_abs_checker", now, now + timedelta(days=7), None, None
        ),

        # Planted 5: User whose grant was revoked mid-work (Anand Verma)
        (
            "grnt_revoked_chk", "usr_revoked", "Checker", None, None, None, None, "usr_admin_1",
            "direct", None, now - timedelta(days=30), None, now - timedelta(days=1), "Transferred to non-customs department"
        ),

        # Planted 8 (Item 2): Deactivated leaver with revoked grant (Dev Patel)
        (
            "grnt_leaver_maker", "usr_leaver", "Maker", None, "MUM", None, None, "usr_admin_1",
            "direct", None, now - timedelta(days=30), now - timedelta(days=10), now - timedelta(days=10), "Left the company"
        ),

        # Planted 8b (Item 2): Deactivated leaver with active, unrevoked grant (Karan Kapoor)
        (
            "grnt_leaver_active_maker", "usr_leaver_active", "Maker", None, "MUM", None, None, "usr_admin_1",
            "direct", None, now - timedelta(days=30), None, None, None
        ),

        # Planted Client-side users (strictly their own client, ClientUser role)
        ("grnt_cli_acme", "usr_client_acme", "ClientUser", "CL-001", None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_cli_zenith", "usr_client_zenith", "ClientUser", "CL-002", None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Planted Overnight parser account (Parser role, Confirmed/Prepared only)
        ("grnt_parser", "usr_parser", "Parser", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),

        # Other standard staff grants
        ("grnt_maker_del", "usr_maker_del", "Maker", None, "DEL", None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_maker_mum", "usr_maker_mum", "Maker", None, "MUM", None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_checker_mum", "usr_checker_mum", "Checker", None, "MUM", None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_maker_all", "usr_maker_all", "Maker", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_checker_all", "usr_checker_all", "Checker", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_filer_all", "usr_filer_all", "Filer", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_auditor", "usr_auditor", "Auditor", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
        ("grnt_trainee", "usr_trainee", "Viewer", None, None, None, None, "usr_admin_1", "direct", None, now - timedelta(days=30), None, None, None),
    ]

    for gid, uid, rid, cid, bid, direct, md, gby, src, p_gid, v_from, v_to, r_at, r_reason in grants_data:
        g = Grant(
            id=gid,
            user_id=uid,
            role_id=rid,
            client_id=cid,
            branch_id=bid,
            direction=direct,
            mode=md,
            valid_from=v_from,
            valid_to=v_to,
            revoked_at=r_at,
            revocation_reason=r_reason,
            granted_by=gby,
            source=src,
            parent_grant_id=p_gid,
            created_at=v_from,
        )
        session.add(g)
    session.flush()

    # 7. Planted Test Jobs (15 fixed test cases - Item 5)
    planted_jobs = [
        Job(
            id="JOB-PLANTED-01",
            client_id="CL-001",
            branch_id="DEL",
            direction="IMPORT",
            mode="AIR",
            stage="Confirmed",
            title="Precision Electronic Chips Import via IGI Airport",
            goods_description="Microcontrollers and sensor assemblies",
            test_case_note="Air import under CL-001; allowed for maker_wide, checker_narrow, and checker_air_only",
            version=1,
            locked=False,
            created_at=now - timedelta(days=2),
        ),
        Job(
            id="JOB-PLANTED-02",
            client_id="CL-001",
            branch_id="DEL",
            direction="EXPORT",
            mode="AIR",
            stage="Confirmed",
            title="Smart Meters Air Export to Frankfurt",
            goods_description="IoT smart electricity meters",
            test_case_note="Air export under CL-001; tests direction restriction (checker_narrow denied on EXPORT)",
            version=1,
            locked=False,
            created_at=now - timedelta(days=2),
        ),
        Job(
            id="JOB-PLANTED-03",
            client_id="CL-001",
            branch_id="MUM",
            direction="IMPORT",
            mode="AIR",
            stage="Confirmed",
            title="Automated Test Equipment Import at Mumbai Air",
            goods_description="Semiconductor testing jigs",
            test_case_note="Mumbai job; tests branch boundary (Delhi-scoped staff denied in Mumbai)",
            version=1,
            locked=False,
            created_at=now - timedelta(days=2),
        ),
        Job(
            id="JOB-PLANTED-04",
            client_id="CL-003",
            branch_id="DEL",
            direction="IMPORT",
            mode="AIR",
            stage="Confirmed",
            title="Auto Engine Control Units for Bharat Auto",
            goods_description="Electronic powertrain modules",
            test_case_note="CL-003 job; tests client restriction (maker_wide can prepare, but cannot check)",
            version=1,
            locked=False,
            created_at=now - timedelta(days=2),
        ),
        Job(
            id="JOB-PLANTED-05",
            client_id="CL-001",
            branch_id="DEL",
            direction="IMPORT",
            mode="AIR",
            stage="Prepared",
            title="Medical Monitors Checklist Prepared by Rohan",
            goods_description="Patient monitoring displays",
            prepared_by="usr_maker_wide",  # Maker-checker conflict test!
            test_case_note="Prepared by Rohan; tests Maker-Checker conflict (preparer denied check despite qualification)",
            version=2,
            locked=False,
            created_at=now - timedelta(days=3),
        ),
        Job(
            id="JOB-PLANTED-06",
            client_id="CL-001",
            branch_id="DEL",
            direction="IMPORT",
            mode="FCL",
            stage="Confirmed",
            title="Heavy Machinery Ocean Freight for Acme",
            goods_description="Industrial CNC milling equipment in 40ft container",
            test_case_note="Ocean FCL import for CL-001; tests mode restriction (checker_air_only denied on non-AIR mode)",
            version=1,
            locked=False,
            created_at=now - timedelta(days=3),
        ),
        Job(
            id="JOB-PLANTED-07",
            client_id="CL-001",
            branch_id="DEL",
            direction="IMPORT",
            mode="FCL",
            stage="Checked",
            title="Industrial Cleanroom Components Verified",
            goods_description="Modular wall panels and HEPA filtration",
            prepared_by="usr_maker_del",
            checked_by="usr_checker_del",
            test_case_note="Customs checklist verified; ready for final filing by staff_filer_all",
            version=3,
            locked=False,
            created_at=now - timedelta(days=4),
        ),
        Job(
            id="JOB-PLANTED-08",
            client_id="CL-001",
            branch_id="DEL",
            direction="IMPORT",
            mode="FCL",
            stage="Filed",
            title="Completed Customs Entry Bill #88219",
            goods_description="Machinery spare parts",
            prepared_by="usr_maker_del",
            checked_by="usr_checker_del",
            test_case_note="Filed customs bill for CL-001; tests Manager dual-control cancellation and Acme client view",
            version=4,
            locked=True,
            locked_reason="Filed to Indian Customs ICEGATE",
            created_at=now - timedelta(days=5),
        ),
        Job(
            id="JOB-PLANTED-09",
            client_id="CL-002",
            branch_id="MUM",
            direction="EXPORT",
            mode="LCL",
            stage="Filed",
            title="Completed Export Shipping Bill #44102",
            goods_description="Consumer audio devices",
            prepared_by="usr_maker_mum",
            checked_by="usr_checker_mum",
            test_case_note="Filed export bill for CL-002; tests client isolation (Zenith views, Acme denied) and parser lock",
            version=4,
            locked=True,
            locked_reason="Filed to Indian Customs ICEGATE",
            created_at=now - timedelta(days=6),
        ),
        Job(
            id="JOB-PLANTED-10",
            client_id="CL-004",
            branch_id="DEL",
            direction="EXPORT",
            mode="AIR",
            stage="Cancelled",
            title="Cancelled Batch of Sterile Syringes",
            goods_description="Medical consumables with cold chain",
            test_case_note="Cancelled job; tests dual-control reopen flow requiring Manager approval",
            version=2,
            locked=True,
            locked_reason="Cancelled by client request prior to filing",
            created_at=now - timedelta(days=7),
        ),
        Job(
            id="JOB-PLANTED-11",
            client_id="CL-005",
            branch_id="KOL",
            direction="IMPORT",
            mode="FCL",
            stage="Reopened",
            title="Reopened Textile Loom Spares under ICEGATE Amendment",
            goods_description="High-precision spinning gears",
            prepared_by="usr_maker_all",
            test_case_note="Reopened job in-flight under approved amendment APPR-001",
            version=5,
            locked=False,
            locked_reason="Reopened under dual approval APPR-001",
            created_at=now - timedelta(days=8),
        ),
        Job(
            id="JOB-PLANTED-12",
            client_id="CL-006",
            branch_id="BLR",
            direction="IMPORT",
            mode="AIR",
            stage="Prepared",
            title="Photovoltaic Inverter Chips - Assigned to Absent Vikram",
            goods_description="Solar power management ICs",
            prepared_by="usr_absent",
            test_case_note="Prepared by Vikram; tests absence handling and routing to delegate Sunita",
            version=2,
            locked=False,
            created_at=now - timedelta(days=2),
        ),
        Job(
            id="JOB-PLANTED-13",
            client_id="CL-007",
            branch_id="MAA",
            direction="EXPORT",
            mode="FCL",
            stage="Prepared",
            title="Polymer Additives - Handled by Revoked Anand",
            goods_description="Specialty chemical stabilizers",
            prepared_by="usr_revoked",
            test_case_note="In-flight job handled by Anand; tests changed-responsibility and reassignment flagging",
            version=2,
            locked=False,
            created_at=now - timedelta(days=2),
        ),
        Job(
            id="JOB-PLANTED-14",
            client_id="CL-008",
            branch_id="MUM",
            direction="IMPORT",
            mode="FCL",
            stage="Prepared",
            title="Hydraulic Valve Replacements - Handled by Leaver Dev",
            goods_description="Heavy crane fluid cylinders",
            prepared_by="usr_leaver",
            test_case_note="In-flight job handled by leaver Dev Patel; tests leaver deactivation and reassignment",
            version=2,
            locked=False,
            created_at=now - timedelta(hours=12),
        ),
        Job(
            id="JOB-PLANTED-15",
            client_id="CL-001",
            branch_id="DEL",
            direction="IMPORT",
            mode="LCL",
            stage="Confirmed",
            title="Acme High Security Hardware Consignment from EDI Feed",
            goods_description="Brass keyed padlocks and chains",
            test_case_note="Early-stage import from EDI; tests automated parser creation/update at Confirmed stage",
            version=1,
            locked=False,
            created_at=now - timedelta(hours=4),
        ),
    ]
    session.add_all(planted_jobs)
    session.flush()

    # 8. Remaining deliberate jobs (285 jobs across all combinations, exactly 300 total)
    stages = ["Confirmed", "Prepared", "Checked", "Filed", "Cancelled", "Reopened"]
    stage_weights = [0.35, 0.25, 0.20, 0.15, 0.03, 0.02]
    directions = ["IMPORT", "EXPORT"]
    modes = ["AIR", "FCL", "LCL"]
    client_ids = [c[0] for c in clients_data]
    branch_ids = ["DEL", "MUM", "KOL", "MAA", "BLR"]

    rng = random.Random(42)  # Fixed seed for repeatability
    additional_jobs = []

    for i in range(16, 301):
        jid = f"JOB-GEN-{i:04d}"
        c_id = rng.choice(client_ids)
        b_id = rng.choice(branch_ids)
        d_val = rng.choice(directions)
        m_val = rng.choice(modes)
        stg = rng.choices(stages, weights=stage_weights)[0]

        is_locked = stg in ["Filed", "Cancelled"]
        lock_msg = f"Locked in {stg} stage" if is_locked else None

        prep_by = None
        chk_by = None
        ver = 1

        if stg in ["Prepared", "Checked", "Filed", "Reopened"]:
            prep_by = "usr_maker_all" if rng.random() > 0.5 else "usr_maker_del"
            ver = 2

        if stg in ["Checked", "Filed"]:
            chk_by = "usr_checker_all" if rng.random() > 0.5 else "usr_checker_mum"
            ver = 3

        if stg == "Filed":
            ver = 4

        j = Job(
            id=jid,
            client_id=c_id,
            branch_id=b_id,
            direction=d_val,
            mode=m_val,
            stage=stg,
            title=f"Shipment {jid} for {c_id}",
            goods_description=f"Standard commercial consignment for {c_id} via {b_id}",
            test_case_note=None,
            prepared_by=prep_by,
            checked_by=chk_by,
            version=ver,
            locked=is_locked,
            locked_reason=lock_msg,
            created_at=now - timedelta(days=rng.randint(1, 45)),
        )
        additional_jobs.append(j)

    session.add_all(additional_jobs)
    session.flush()

    # 9. Planted Assignments
    assignments = [
        Assignment(
            id="asgn_01",
            job_id="JOB-PLANTED-01",
            user_id="usr_maker_del",
            assigned_role="Maker",
            is_active=True,
            needs_reassignment=False,
            assigned_at=now - timedelta(days=2),
        ),
        Assignment(
            id="asgn_05",
            job_id="JOB-PLANTED-05",
            user_id="usr_checker_del",
            assigned_role="Checker",
            is_active=True,
            needs_reassignment=False,
            assigned_at=now - timedelta(days=1),
        ),
        Assignment(
            id="asgn_12_absent",
            job_id="JOB-PLANTED-12",
            user_id="usr_absent",
            assigned_role="Maker",
            is_active=True,
            needs_reassignment=True,
            reassignment_reason="User Vikram Malhotra is absent on medical leave",
            assigned_at=now - timedelta(days=2),
        ),
        Assignment(
            id="asgn_13_revoked",
            job_id="JOB-PLANTED-13",
            user_id="usr_revoked",
            assigned_role="Maker",
            is_active=False,
            needs_reassignment=True,
            reassignment_reason="User Anand Verma grant was revoked due to transfer",
            assigned_at=now - timedelta(days=3),
        ),
        Assignment(
            id="asgn_14_leaver",
            job_id="JOB-PLANTED-14",
            user_id="usr_leaver",
            assigned_role="Maker",
            is_active=False,
            needs_reassignment=True,
            reassignment_reason="User Dev Patel left the company",
            assigned_at=now - timedelta(days=4),
        ),
    ]
    session.add_all(assignments)
    session.flush()

    # 10. Planted Approvals (Dual control: Q6, Q8, Item 7)
    approvals = [
        Approval(
            id="appr_reopen_001",
            action="job:reopen_closed",
            target_type="JOB_REOPEN",
            target_id="JOB-PLANTED-11",
            requested_by="usr_manager",
            approved_by="usr_admin_1",
            reason="Customs amendment permitted under ICEGATE notification 44/2026",
            status="used",
            approved_at=now - timedelta(days=2),
            expires_at=now + timedelta(days=3),
            used_at=now - timedelta(days=1),
            is_active=True,
            created_at=now - timedelta(days=2),
        ),
        Approval(
            id="appr_pending_cancel",
            action="job:cancel_filed",
            target_type="JOB_CANCEL_FILED",
            target_id="JOB-PLANTED-08",
            requested_by="usr_manager",
            approved_by=None,  # Pending second approver sign-off!
            reason="Client Acme requested post-filing recall due to shipment dispute",
            status="pending",
            approved_at=None,
            expires_at=now + timedelta(days=1),
            used_at=None,
            is_active=True,
            created_at=now - timedelta(hours=3),
        ),
    ]
    session.add_all(approvals)
    session.flush()

    # 11. Initial Audit Log with Hash Chaining
    # Genesis block: prev_hash = "0"*64
    prev_h = "0" * 64
    audit_entries_raw = [
        ("GRANT_CHANGE", "usr_admin_2", "grant", "grnt_admin_1", "CREATE_GRANT", "ALLOW", None,
         ["Dual control: admin_2 granted Admin role to admin_1"], ["grnt_admin_2"],
         {"role": "Admin", "scope": "Global"}),
        ("GRANT_CHANGE", "usr_admin_1", "grant", "grnt_manager_del", "CREATE_GRANT", "ALLOW", None,
         ["Granted Manager role for Delhi hub to Vikrant Deshmukh"], ["grnt_admin_1"],
         {"role": "Manager", "scope": "DEL"}),
        ("STAGE_TRANSITION", "usr_maker_wide", "job", "JOB-PLANTED-05", "PREPARE", "EXECUTED", None,
         ["Rohan Mehta prepared customs checklist for JOB-PLANTED-05"], ["grnt_mwcf_maker"],
         {"from_stage": "Confirmed", "to_stage": "Prepared"}),
        ("HIGH_RISK_ACTION", "usr_admin_1", "approval", "appr_reopen_001", "APPROVE_REOPEN", "ALLOW", None,
         ["Admin approved Manager reopening request for JOB-PLANTED-11"], ["grnt_admin_1"],
         {"target_job": "JOB-PLANTED-11"}),
    ]

    for etype, act_id, tgt_type, tgt_id, actn, dec, dcode, reasons, g_ids, snap in audit_entries_raw:
        rec_hash = AuditLog.compute_hash(
            prev_hash=prev_h,
            timestamp=now,
            event_type=etype,
            actor_id=act_id,
            target_type=tgt_type,
            target_id=tgt_id,
            action=actn,
            decision=dec,
            deny_code=dcode,
            reasons=json.dumps(reasons),
            grant_ids_used=json.dumps(g_ids),
            permission_snapshot=json.dumps(snap),
            details=None,
        )
        audit_entry = AuditLog(
            timestamp=now,
            event_type=etype,
            actor_id=act_id,
            target_type=tgt_type,
            target_id=tgt_id,
            action=actn,
            decision=dec,
            deny_code=dcode,
            reasons=json.dumps(reasons),
            grant_ids_used=json.dumps(g_ids),
            permission_snapshot=json.dumps(snap),
            prev_hash=prev_h,
            record_hash=rec_hash,
        )
        session.add(audit_entry)
        session.flush()
        prev_h = rec_hash

    session.commit()

    return {
        "branches": len(branches),
        "clients": len(clients),
        "users": len(users),
        "roles": len(roles_def),
        "permissions": len(permissions),
        "grants": len(grants_data),
        "jobs": len(planted_jobs) + len(additional_jobs),
        "planted_jobs": len(planted_jobs),
        "assignments": len(assignments),
        "approvals": len(approvals),
        "audit_logs": len(audit_entries_raw),
    }
