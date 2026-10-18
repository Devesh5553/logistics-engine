from sqlalchemy.orm import Session
from src.models import User, Grant, Branch, Client, Role, Permission, RolePermission, Job


def generate_branches_table(session: Session) -> str:
    """Generates markdown table of branch codes and names."""
    branches = session.query(Branch).order_by(Branch.id).all()
    lines = [
        "| Branch Code | Hub Name | City | Standard Convention |",
        "| :--- | :--- | :--- | :--- |"
    ]
    for b in branches:
        lines.append(f"| `{b.id}` | {b.name} | {b.city} | 3-letter IATA / Hub Code |")
    return "\n".join(lines)


def generate_clients_table(session: Session) -> str:
    """Generates markdown table of client IDs and full enterprise names."""
    clients = session.query(Client).order_by(Client.id).all()
    lines = [
        "| Client ID | Client Enterprise Name | Industry | Status |",
        "| :--- | :--- | :--- | :--- |"
    ]
    for c in clients:
        status = "Active" if c.is_active else "Inactive"
        lines.append(f"| `{c.id}` | **{c.name}** | {c.industry or 'General'} | {status} |")
    return "\n".join(lines)


def generate_role_permissions_table(session: Session) -> str:
    """Generates markdown table of roles, permitted actions, and stage limits."""
    roles = session.query(Role).order_by(Role.id).all()
    lines = [
        "| Role | Allowed Actions / Permissions | Stage Limits & Guardrails | Description |",
        "| :--- | :--- | :--- | :--- |"
    ]

    for role in roles:
        rps = (
            session.query(RolePermission)
            .filter(RolePermission.role_id == role.id)
            .all()
        )
        perm_ids = [rp.permission_id for rp in rps]
        perms = (
            session.query(Permission)
            .filter(Permission.id.in_(perm_ids))
            .all()
        ) if perm_ids else []

        perm_str = ", ".join(f"`{p.id}`" for p in perms)
        guardrails = []
        for p in perms:
            if p.requires_second_approver:
                guardrails.append(f"`{p.id}` requires 2nd approver + mandatory reason")
            elif p.stage_limits and p.stage_limits != "All stages" and p.stage_limits != "N/A":
                guardrails.append(f"`{p.id}` limited to {p.stage_limits}")

        # Specific role summaries for parser and client user
        if role.id == "Parser":
            guardrail_str = "**Strict:** Create/Prepare at Confirmed/Prepared only. CANNOT file, cancel, or reopen."
        elif role.id == "ClientUser":
            guardrail_str = "**Strict:** Read-only for own client. Internal notes & other clients hidden."
        elif guardrails:
            guardrail_str = "; ".join(guardrails)
        else:
            guardrail_str = "Standard stage workflow"

        lines.append(f"| **{role.id}** | {perm_str} | {guardrail_str} | {role.description or ''} |")

    return "\n".join(lines)


def generate_planted_jobs_table(session: Session) -> str:
    """Generates markdown table of the 15 deliberate planted test jobs."""
    jobs = (
        session.query(Job)
        .filter(Job.id.like("JOB-PLANTED-%"))
        .order_by(Job.id.asc())
        .all()
    )

    clients_map = {c.id: c.name for c in session.query(Client).all()}
    users_map = {u.id: u.username for u in session.query(User).all()}

    lines = [
        "| Job ID | Client | Branch | Dir | Mode | Stage | Prepared By | Test Case / Policy Tested |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    ]

    for j in jobs:
        c_name = clients_map.get(j.client_id, j.client_id)
        prep = users_map.get(j.prepared_by, "-") if j.prepared_by else "-"
        note = j.test_case_note or "Planted scenario"
        lines.append(
            f"| `{j.id}` | `{j.client_id}` ({c_name}) | `{j.branch_id}` | `{j.direction}` | `{j.mode}` | **{j.stage}** | {prep} | {note} |"
        )

    return "\n".join(lines)


def generate_access_lines(session: Session) -> list[str]:
    """
    Generates plain-English descriptions of each person's access,
    grouped one person per entry with indented grants beneath (Item 9).
    """
    users = session.query(User).order_by(User.user_type, User.username).all()
    clients_map = {c.id: c.name for c in session.query(Client).all()}
    lines = []

    for user in users:
        # Determine user header badges
        status_badges = []
        if not user.is_active:
            deact_date = user.deactivated_at.strftime("%Y-%m-%d") if user.deactivated_at else "prior"
            status_badges.append(f"DEACTIVATED on {deact_date}")
        elif user.is_absent:
            abs_from = user.absent_from.strftime("%Y-%m-%d") if user.absent_from else "now"
            abs_to = user.absent_to.strftime("%Y-%m-%d") if user.absent_to else "indefinite"
            status_badges.append(f"ABSENT: {abs_from} to {abs_to} ({user.absence_reason or 'No reason'})")
        else:
            status_badges.append("Active")

        badge_str = f" [{', '.join(status_badges)}]"
        user_header = f"- **{user.full_name}** (`{user.username}`){badge_str}"
        lines.append(user_header)

        grants = (
            session.query(Grant)
            .filter(Grant.user_id == user.id)
            .order_by(Grant.created_at.asc())
            .all()
        )

        if not grants:
            lines.append("  - (No grants assigned)")
            continue

        for g in grants:
            c_str = f"{g.client_id} ({clients_map.get(g.client_id, '')})" if g.client_id else "all clients"
            b_str = g.branch_id if g.branch_id else "all branches"
            d_str = g.direction if g.direction else "all directions"
            m_str = g.mode if g.mode else "all modes"

            grant_notes = []
            if g.source == "delegation":
                v_from_str = g.valid_from.strftime("%Y-%m-%d") if g.valid_from else "start"
                v_to_str = g.valid_to.strftime("%Y-%m-%d") if g.valid_to else "end"
                grant_notes.append(f"delegated from absent colleague Vikram Malhotra; valid from {v_from_str} to {v_to_str}, valid for 7 days from seeding")
            elif g.revoked_at:
                grant_notes.append(f"REVOKED: {g.revocation_reason or 'No reason provided'}")
            elif not user.is_active and not g.revoked_at:
                grant_notes.append("Active grant unrevoked: isolates inactive user check")
            elif g.valid_to:
                grant_notes.append(f"valid until {g.valid_to.strftime('%Y-%m-%d')}")

            notes_str = f" ({', '.join(grant_notes)})" if grant_notes else ""
            grant_line = (
                f"  - Role **{g.role_id}** for client *{c_str}*, branch *{b_str}*, "
                f"direction *{d_str}*, mode *{m_str}*{notes_str}"
            )
            lines.append(grant_line)

    return lines


def generate_readme_access_markdown(session: Session) -> str:
    lines = generate_access_lines(session)
    header = "<!-- ACCESS_LINES_START -->\n"
    footer = "\n<!-- ACCESS_LINES_END -->"
    content = "\n".join(lines)
    return f"{header}{content}{footer}"


def replace_marked_section(text: str, start_marker: str, end_marker: str, new_content: str) -> str:
    if start_marker in text and end_marker in text:
        before = text.split(start_marker)[0]
        after = text.split(end_marker)[1]
        return f"{before}{start_marker}\n{new_content}\n{end_marker}{after}"
    return text


def update_readme_file(readme_path: str, session: Session) -> None:
    with open(readme_path, "r", encoding="utf-8") as f:
        text = f.read()

    # 1. Update Branches Table
    branches_table = generate_branches_table(session)
    text = replace_marked_section(text, "<!-- BRANCHES_TABLE_START -->", "<!-- BRANCHES_TABLE_END -->", branches_table)

    # 2. Update Clients Table
    clients_table = generate_clients_table(session)
    text = replace_marked_section(text, "<!-- CLIENTS_TABLE_START -->", "<!-- CLIENTS_TABLE_END -->", clients_table)

    # 3. Update Role Permissions Table
    roles_table = generate_role_permissions_table(session)
    text = replace_marked_section(text, "<!-- ROLE_PERMISSIONS_START -->", "<!-- ROLE_PERMISSIONS_END -->", roles_table)

    # 4. Update Planted Jobs Table
    jobs_table = generate_planted_jobs_table(session)
    text = replace_marked_section(text, "<!-- PLANTED_JOBS_START -->", "<!-- PLANTED_JOBS_END -->", jobs_table)

    # 5. Update Access Lines
    access_block = "\n".join(generate_access_lines(session))
    text = replace_marked_section(text, "<!-- ACCESS_LINES_START -->", "<!-- ACCESS_LINES_END -->", access_block)

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(text)
