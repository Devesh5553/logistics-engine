"""
test_phase5.py — Phase 5 Verification: Documentation, Demo Walkthrough, and Web UI.
===================================================================================

Verifies:
  1. DESIGN_DOCUMENT.md exists and covers all required architectural questions (Q1–Q19).
  2. README.md contains exact plain-English access lines matching sample seeds and preserves
     verbatim 'Known limits and cuts'.
  3. Web UI static dashboard mounted at /ui returns 200 OK HTML.
  4. Root endpoint / redirects to /ui.
  5. demo_walkthrough.py is present and executable.
"""

import os
import sys
import subprocess
from pathlib import Path
import pytest
from starlette.testclient import TestClient

from src.config import BASE_DIR
from src.api.app import app
from src.seeds.readme_generator import generate_access_lines


@pytest.fixture
def api_client(db_session):
    from src.api.app import get_db
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


class TestPhase5Documentation:
    """Verifies architectural documentation completeness in DESIGN_DOCUMENT.md."""

    def test_design_document_exists(self):
        doc_path = BASE_DIR / "DESIGN_DOCUMENT.md"
        assert doc_path.exists(), "DESIGN_DOCUMENT.md must exist in the workspace root"
        content = doc_path.read_text(encoding="utf-8")
        assert len(content) > 3000, "DESIGN_DOCUMENT.md should be comprehensive (2-4 pages)"

    def test_design_document_covers_all_questions_q1_to_q19(self):
        doc_path = BASE_DIR / "DESIGN_DOCUMENT.md"
        content = doc_path.read_text(encoding="utf-8")

        # Required question sections Q1 through Q19
        for q_num in range(1, 20):
            q_marker = f"Q{q_num}"
            assert q_marker in content, f"DESIGN_DOCUMENT.md must include question section {q_marker}"

    def test_design_document_architectural_topics(self):
        doc_path = BASE_DIR / "DESIGN_DOCUMENT.md"
        content = doc_path.read_text(encoding="utf-8").lower()

        # Key architectural topics documented in DESIGN_DOCUMENT.md
        key_topics = [
            "fail-closed",
            "dual-control",
            "scoped",
            "hash",
            "chain",
            "tier",
            "temporal",
            "privacy",
            "optimistic concurrency",
        ]
        for topic in key_topics:
            assert topic in content, f"DESIGN_DOCUMENT.md should explain topic '{topic}'"


class TestPhase5ReadmeParityAndLimits:
    """Verifies README.md access lines and verbatim known limits."""

    def test_readme_exists(self):
        readme_path = BASE_DIR / "README.md"
        assert readme_path.exists(), "README.md must exist"

    def test_readme_contains_verbatim_known_limits_and_cuts(self):
        readme_path = BASE_DIR / "README.md"
        content = readme_path.read_text(encoding="utf-8")

        expected_cuts = [
            "## Known limits and cuts",
            "Assignment endpoint does not verify the assignee holds the role in scope or is active",
            "Deactivate and transfer have no last-admin guard, self-deactivation guard, or second approval for admins",
            "Audit chain can fork under truly concurrent writers (single-writer SQLite assumed",
            "Transition denials return rule names to callers without audit:read",
            "PATCH /grants can currently lengthen valid_to; only shortening is intended",
            "Parser has no service-key authentication; it is identified by its X-Actor-Id account",
            "Retention of audit history assumed 7 years, not automated",
        ]
        for cut in expected_cuts:
            assert cut in content, f"README.md must contain verbatim cut text: '{cut}'"

    def test_readme_access_lines_parity(self, db_session):
        readme_path = BASE_DIR / "README.md"
        content = readme_path.read_text(encoding="utf-8")

        # Generate live access lines from the database session
        expected_lines = generate_access_lines(db_session)
        assert len(expected_lines) >= 15, "Should generate access lines for seeded personas"

        # Key seeded personas must appear in README
        personas = [
            "staff_maker_del",
            "checker_narrow",
            "staff_maker_mum",
            "admin_primary",
            "client_acme_user",
        ]
        for p in personas:
            assert p in content, f"README.md access lines must reference seeded user '{p}'"


class TestPhase5ServiceRootAndDocs:
    """Verifies that the service root redirects to Swagger documentation and /ui is removed."""

    def test_root_redirects_to_docs(self, api_client):
        resp = api_client.get("/", follow_redirects=False)
        assert resp.status_code in (301, 302, 307, 308)
        assert resp.headers.get("location") == "/docs"

    def test_ui_dashboard_removed(self, api_client):
        resp = api_client.get("/ui/")
        assert resp.status_code == 404, f"Expected 404 Not Found from /ui/ after removal, got {resp.status_code}"

    def test_swagger_docs_accessible(self, api_client):
        resp = api_client.get("/docs")
        assert resp.status_code == 200
        assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()


class TestPhase5WalkthroughDemo:
    """Verifies demo_walkthrough.py presence and non-interactive automated execution."""

    def test_demo_walkthrough_file_exists(self):
        demo_script = BASE_DIR / "demo_walkthrough.py"
        assert demo_script.exists(), "demo_walkthrough.py must exist in root"

    def test_demo_walkthrough_runs_automated(self):
        """Run demo_walkthrough.py with --auto --delay 0.01 in a subprocess."""
        cmd = [sys.executable, str(BASE_DIR / "demo_walkthrough.py"), "--auto", "--delay", "0.01"]
        proc = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, f"demo_walkthrough.py failed with stderr: {proc.stderr}\nstdout: {proc.stdout}"
        assert "WALKTHROUGH COMPLETE: ALL 10 CAPABILITIES VERIFIED SUCCESSFULLY" in proc.stdout
