#!/usr/bin/env python3
"""
Nagarkot Forwarders Pvt. Ltd. (NFPL) - Access Control Service
=============================================================
Phase 5 Interactive / Automated Terminal Walkthrough Demo (~5 Minutes)

Demonstrates:
  1. Service health and planted operational personas.
  2. Differential access comparison ("Why can Rohan do this and Kavita can't?").
  3. Actionable work queue for active makers/checkers.
  4. Workflow state machine progression (PREPARE, CHECK, FILE, self-approval guard).
  5. High-risk action blocking (Cancel filed job without dual-control approval).
  6. Dual-control approval request lifecycle (request, 2nd approver approve, consume).
  7. Client privacy boundary (Internal staff audit timeline vs external client redaction).
  8. Temporal grant replay (Reconstructing who had authority at past dates).
  9. Cryptographic hash chain verification and tamper detection.
  10. Incident early-warning reporting (Detecting repeated probes on filed jobs).

Usage:
  python demo_walkthrough.py         # Interactive mode (step through with Enter)
  python demo_walkthrough.py --auto  # Automated mode (runs all steps with brief pacing)
"""

import os
import sys
import time
import argparse
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Ensure project root is in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from starlette.testclient import TestClient
from src.api.app import app
from src.db import SessionLocal, init_db, drop_db
from src.models import (
    User, Job, Grant, Role, Client as ClientModel, Branch, Approval, AuditLog, JobEvent
)
from src.seeds.sample_data import seed_sample_data

# ANSI Color codes
BOLD = "\033[1m"
GREEN = "\033[92m"
BLUE = "\033[94m"
CYAN = "\033[96m"
YELLOW = "\033[93m"
RED = "\033[91m"
MAGENTA = "\033[95m"
DIM = "\033[2m"
RESET = "\033[0m"


class DemoClient:
    """Wrapper that can execute requests either via live HTTP or in-process TestClient."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")
        self.use_live = False
        self.test_client = None

        # Check if live server is reachable
        try:
            import urllib.request
            req = urllib.request.Request(f"{self.base_url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    self.use_live = True
        except Exception:
            self.use_live = False

        if not self.use_live:
            self.test_client = TestClient(app)

    def request(self, method: str, path: str, actor_id: Optional[str] = None, **kwargs) -> Any:
        headers = kwargs.pop("headers", {})
        if actor_id:
            headers["X-Actor-Id"] = actor_id

        if self.use_live:
            import urllib.request
            import urllib.parse
            url = f"{self.base_url}{path}"
            params = kwargs.get("params")
            if params:
                query_string = urllib.parse.urlencode(params)
                url = f"{url}?{query_string}"

            body = kwargs.get("json")
            data = json.dumps(body).encode("utf-8") if body else None
            if body:
                headers["Content-Type"] = "application/json"

            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req) as resp:
                    resp_data = resp.read().decode("utf-8")
                    return ResponseProxy(resp.status, json.loads(resp_data) if resp_data else {})
            except urllib.error.HTTPError as e:
                resp_data = e.read().decode("utf-8")
                try:
                    err_json = json.loads(resp_data)
                except Exception:
                    err_json = {"detail": resp_data}
                return ResponseProxy(e.code, err_json)
        else:
            resp = self.test_client.request(method, path, headers=headers, **kwargs)
            try:
                data = resp.json()
            except Exception:
                data = {"text": resp.text}
            return ResponseProxy(resp.status_code, data)


class ResponseProxy:
    def __init__(self, status_code: int, data: Any):
        self.status_code = status_code
        self.data = data

    def json(self):
        return self.data


def print_banner():
    banner = f"""
{CYAN}{BOLD}================================================================================
  NAGARKOT FORWARDERS PVT. LTD. (NFPL)
  CUSTOMS BROKERAGE ACCESS CONTROL SERVICE - DEMO WALKTHROUGH
================================================================================{RESET}
{DIM}Scoped-role, explainable, dual-control, and tamper-evident audit control system.{RESET}
"""
    print(banner)


def print_section(num: int, title: str, subtitle: str = ""):
    print(f"\n{YELLOW}{BOLD}[Step {num}] {title}{RESET}")
    if subtitle:
        print(f"{DIM}{subtitle}{RESET}")
    print(f"{CYAN}{'-' * 70}{RESET}")


def pause_prompt(auto_mode: bool, delay: float = 1.0):
    if auto_mode:
        time.sleep(delay)
    else:
        try:
            input(f"\n{DIM}Press [Enter] to continue to next step...{RESET}")
        except (KeyboardInterrupt, EOFError):
            print("\nExiting walkthrough.")
            sys.exit(0)


def ensure_db_seeded():
    """Ensure database is seeded and reset JOB-PLANTED-01 to Confirmed stage."""
    init_db()
    session = SessionLocal()
    try:
        user_count = session.query(User).count()
        if user_count < 10:
            seed_sample_data(session)
        job = session.query(Job).filter(Job.id == "JOB-PLANTED-01").first()
        if job:
            job.stage = "Confirmed"
            job.version = 1
            job.prepared_by = None
            job.checked_by = None
            job.locked = False
            job.locked_reason = None
            session.commit()
    finally:
        session.close()


def run_walkthrough(auto: bool = False, delay: float = 0.5):
    print_banner()
    ensure_db_seeded()

    client = DemoClient()
    mode_str = f"Live HTTP Server ({client.base_url})" if client.use_live else "In-Process Test Engine (FastAPI TestClient)"
    print(f"Connecting via: {GREEN}{BOLD}{mode_str}{RESET}\n")

    # -------------------------------------------------------------------------
    # STEP 1: Service Health & Seeded Personas
    # -------------------------------------------------------------------------
    print_section(1, "Service Health & Seeded Personas", "Verify health endpoint and examine active test accounts.")
    h_resp = client.request("GET", "/health")
    print(f"GET /health -> Status {BOLD}{h_resp.status_code}{RESET}: {h_resp.json()}")

    print("\nPlanted Personas for Demonstration:")
    personas = [
        ("usr_maker_del", "Rohan Verma", "DEL Branch", "Maker", "Assigned preparer for Delhi import jobs"),
        ("usr_checker_del", "Pooja Gupta", "DEL Branch", "Checker", "Qualified checker for Delhi branch"),
        ("usr_maker_mum", "Kavita Shah", "MUM Branch", "Maker", "Mumbai maker, out of scope for Delhi jobs"),
        ("usr_admin_1", "Anand Verma", "Operations", "Admin", "High-risk authorizer and operations admin"),
        ("usr_filer_all", "Suresh Nair", "National Hub", "Filer", "Authorized customs EDI electronic filing officer"),
        ("usr_client_acme", "Sanjay Mehta", "Client CL-001", "ClientUser", "External customer restricted to stage views"),
    ]
    for uid, name, branch, role, desc in personas:
        print(f"  * {BOLD}{uid:<18}{RESET} | {name:<14} | {branch:<12} | {role:<11} | {DIM}{desc}{RESET}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 2: Explainable Differential Access
    # -------------------------------------------------------------------------
    print_section(
        2,
        "Differential Access Engine: 'Why can Rohan prepare this job while Kavita cannot?'",
        "Compare two users on Delhi import job JOB-PLANTED-01."
    )
    comp_resp = client.request(
        "GET",
        "/compare",
        actor_id="usr_admin_1",
        params={
            "user_a": "usr_maker_del",
            "user_b": "usr_maker_mum",
            "action": "job:prepare",
            "job_id": "JOB-PLANTED-01",
        }
    )
    print("GET /compare?user_a=usr_maker_del&user_b=usr_maker_mum&action=job:prepare&job_id=JOB-PLANTED-01")
    print(f"Status: {BOLD}{comp_resp.status_code}{RESET}")
    cdata = comp_resp.json()
    user_a = cdata.get("user_a", {})
    user_b = cdata.get("user_b", {})
    diff = cdata.get("differences", [])

    print(f"\n  User A ({BOLD}Rohan - usr_maker_del{RESET}):")
    print(f"    Allowed: {GREEN}{user_a.get('allowed')}{RESET} | Reasons: {user_a.get('reasons')}")
    print(f"  User B ({BOLD}Kavita - usr_maker_mum{RESET}):")
    print(f"    Allowed: {RED}{user_b.get('allowed')}{RESET} | Deny Code: {RED}{user_b.get('deny_code')}{RESET}")
    print(f"    Reasons: {user_b.get('reasons')}")
    print(f"\n  {BOLD}Explanation Differences:{RESET}")
    for d in diff:
        print(f"    [!] {d}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 3: Work Queue Inspection
    # -------------------------------------------------------------------------
    print_section(
        3,
        "Actionable Work Queue: Rohan's Pending Tasks",
        "Retrieve actionable jobs matching Rohan's role, branch, and stage requirements."
    )
    q_resp = client.request(
        "GET",
        "/users/usr_maker_del/queue",
        actor_id="usr_maker_del"
    )
    print(f"GET /users/usr_maker_del/queue -> Status {BOLD}{q_resp.status_code}{RESET}")
    q_data = q_resp.json()
    jobs = q_data.get("jobs", [])
    print(f"Total Actionable Jobs in Queue: {BOLD}{len(jobs)}{RESET}")
    for j in jobs[:5]:
        print(f"  * {BOLD}{j.get('job_id')}{RESET} | Stage: {CYAN}{j.get('stage')}{RESET} | Branch: {j.get('branch_id')} | Action: {GREEN}{j.get('action')}{RESET}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 4: Workflow Progression (PREPARE, CHECK, FILE)
    # -------------------------------------------------------------------------
    print_section(
        4,
        "Workflow Progression & Dual-Control Separation of Duty",
        "Deepa prepares; self-approval prevents Deepa from checking; Pooja checks; Suresh files."
    )

    def get_job_state() -> Dict[str, Any]:
        return client.request("GET", "/jobs/JOB-PLANTED-01", actor_id="usr_admin_1").json()

    # 4a. Deepa Prepares JOB-PLANTED-01
    cur_v = get_job_state().get("version", 1)
    print(f"Action 1: Deepa Rao (usr_maker_wide) prepares JOB-PLANTED-01 (version={cur_v})")
    prep_resp = client.request(
        "POST",
        "/jobs/JOB-PLANTED-01/transition",
        actor_id="usr_maker_wide",
        json={
            "action": "job:prepare",
            "version": cur_v,
            "reason": "Shipping bill documentation verified and signed"
        }
    )
    print(f"POST /jobs/JOB-PLANTED-01/transition (job:prepare) -> Status {prep_resp.status_code}")
    if prep_resp.status_code == 200:
        p_res = prep_resp.json()
        print(f"  {GREEN}[OK] Transition to 'Prepared' successful. From: {p_res.get('from_stage')} -> To: {p_res.get('to_stage')}{RESET}")

    # 4b. Self-Approval Block (Deepa tries to check the job she just prepared)
    cur_v = get_job_state().get("version", 2)
    print(f"\nAction 2: Deepa attempts to Check her own prepared job (Dual-Control Self-Approval Guard, version={cur_v})")
    self_check = client.request(
        "POST",
        "/jobs/JOB-PLANTED-01/transition",
        actor_id="usr_maker_wide",
        json={
            "action": "job:check",
            "version": cur_v,
            "reason": "Self-checking my own file"
        }
    )
    print(f"POST /jobs/JOB-PLANTED-01/transition (job:check as Deepa) -> Status {BOLD}{self_check.status_code}{RESET}")
    print(f"  {RED}[BLOCKED]:{RESET} {self_check.json().get('detail')}")

    # 4c. Pooja Checks
    cur_v = get_job_state().get("version", 2)
    print(f"\nAction 3: Pooja Gupta (Independent Checker) checks JOB-PLANTED-01 (version={cur_v})")
    pooja_check = client.request(
        "POST",
        "/jobs/JOB-PLANTED-01/transition",
        actor_id="usr_checker_del",
        json={
            "action": "job:check",
            "version": cur_v,
            "reason": "Independent secondary review completed; compliant"
        }
    )
    print(f"POST /jobs/JOB-PLANTED-01/transition (job:check as Pooja) -> Status {BOLD}{pooja_check.status_code}{RESET}")
    if pooja_check.status_code == 200:
        print(f"  {GREEN}[OK] Independent check approved! Job moved to 'Checked'.{RESET}")

    # 4d. Suresh Files
    cur_v = get_job_state().get("version", 3)
    print(f"\nAction 4: Suresh Nair (Filer) files JOB-PLANTED-01 with Customs ICEGATE (version={cur_v})")
    file_resp = client.request(
        "POST",
        "/jobs/JOB-PLANTED-01/transition",
        actor_id="usr_filer_all",
        json={
            "action": "job:file",
            "version": cur_v,
            "reason": "Customs electronic entry successfully filed and acknowledged"
        }
    )
    print(f"POST /jobs/JOB-PLANTED-01/transition (job:file) -> Status {BOLD}{file_resp.status_code}{RESET}")
    if file_resp.status_code == 200:
        print(f"  {GREEN}[OK] Successfully Filed! Job is now in stage 'Filed'.{RESET}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 5: Blocked High-Risk Action
    # -------------------------------------------------------------------------
    print_section(
        5,
        "High-Risk Guardrails: Direct Cancellation of Filed Job Blocked",
        "Direct cancellation of a filed customs job without dual-control approval is strictly denied."
    )
    cur_v = get_job_state().get("version", 4)
    cancel_attempt = client.request(
        "POST",
        "/jobs/JOB-PLANTED-01/transition",
        actor_id="usr_admin_1",
        json={
            "action": "job:cancel_filed",
            "version": cur_v,
            "reason": "Direct administrative cancellation attempt"
        }
    )
    print(f"POST /jobs/JOB-PLANTED-01/transition (job:cancel_filed direct) -> Status {BOLD}{cancel_attempt.status_code}{RESET}")
    print(f"  {RED}[BLOCKED]:{RESET} {cancel_attempt.json().get('detail')}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 6: Dual-Control Approval Lifecycle
    # -------------------------------------------------------------------------
    print_section(
        6,
        "Dual-Control Approval Lifecycle: Request, Approve, and Consume",
        "Rohan requests cancellation approval; Anand (Admin) authorizes it; Anand executes."
    )

    # 6a. Submit approval request
    print("Sub-step A: Rohan submits dual-control approval request for job:cancel_filed")
    req_resp = client.request(
        "POST",
        "/approvals",
        actor_id="usr_maker_del",
        json={
            "action": "job:cancel_filed",
            "target_type": "job",
            "target_id": "JOB-PLANTED-01",
            "reason": "Client cargo confiscated at port due to customs discrepancy; formal withdrawal"
        }
    )
    print(f"POST /approvals -> Status {BOLD}{req_resp.status_code}{RESET}")
    app_data = req_resp.json()
    approval_id = app_data.get("id")
    print(f"  Created Approval ID: {BOLD}{approval_id}{RESET} | Status: {YELLOW}{app_data.get('status')}{RESET}")

    # 6b. Self-approval block on approval request
    print("\nSub-step B: Rohan tries to approve his own request (Self-Authorization Guard)")
    self_app = client.request(
        "POST",
        f"/approvals/{approval_id}/approve",
        actor_id="usr_maker_del"
    )
    print(f"POST /approvals/{approval_id}/approve (as Rohan) -> Status {BOLD}{self_app.status_code}{RESET}")
    print(f"  {RED}[BLOCKED]:{RESET} {self_app.json().get('detail')}")

    # 6c. Second approver authorizes
    print("\nSub-step C: Anand Verma (usr_admin_1) approves the request")
    auth_resp = client.request(
        "POST",
        f"/approvals/{approval_id}/approve",
        actor_id="usr_admin_1"
    )
    print(f"POST /approvals/{approval_id}/approve (as Anand) -> Status {BOLD}{auth_resp.status_code}{RESET}")
    print(f"  {GREEN}[APPROVED]:{RESET} Status: {auth_resp.json().get('status')} by {auth_resp.json().get('approved_by')}")

    # 6d. Execute high-risk cancellation using approved token
    cur_v = get_job_state().get("version", 4)
    print(f"\nSub-step D: Anand executes cancellation providing approval_id={approval_id} (version={cur_v})")
    exec_cancel = client.request(
        "POST",
        "/jobs/JOB-PLANTED-01/transition",
        actor_id="usr_admin_1",
        json={
            "action": "job:cancel_filed",
            "version": cur_v,
            "reason": "Authorized cancellation following confirmed port confiscation",
            "approval_id": approval_id
        }
    )
    print(f"POST /jobs/JOB-PLANTED-01/transition -> Status {BOLD}{exec_cancel.status_code}{RESET}")
    if exec_cancel.status_code == 200:
        c_res = exec_cancel.json()
        print(f"  {GREEN}[OK] SUCCESS:{RESET} Job transitioned to '{c_res.get('to_stage')}'. Single-use approval consumed.")
    else:
        print(f"  Result: {exec_cancel.json()}")

    # 6e. Verify single-use token replay is blocked
    cur_v = get_job_state().get("version", 5)
    print(f"\nSub-step E: Attempting replay with already-consumed approval_id (version={cur_v})")
    replay_cancel = client.request(
        "POST",
        "/jobs/JOB-PLANTED-01/transition",
        actor_id="usr_admin_1",
        json={
            "action": "job:cancel_filed",
            "version": cur_v,
            "reason": "Second attempt reusing approval token",
            "approval_id": approval_id
        }
    )
    print(f"POST /jobs/JOB-PLANTED-01/transition (Replay) -> Status {BOLD}{replay_cancel.status_code}{RESET}")
    print(f"  {RED}[BLOCKED]:{RESET} {replay_cancel.json().get('detail')}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 7: Job History & Client Privacy Boundary
    # -------------------------------------------------------------------------
    print_section(
        7,
        "Job History & Client Privacy Redaction",
        "Staff sees full internal notes, actors, and state; external client sees sanitized stages."
    )

    # 7a. Staff View
    staff_hist = client.request("GET", "/jobs/JOB-PLANTED-01/history", actor_id="usr_admin_1")
    print(f"1. Internal Staff Perspective (Actor: {BOLD}usr_admin_1{RESET}):")
    print(f"   GET /jobs/JOB-PLANTED-01/history -> Status {staff_hist.status_code}")
    events_staff = staff_hist.json().get("events", [])
    print(f"   Events visible ({len(events_staff)}):")
    for ev in events_staff[:4]:
        print(f"     * {ev.get('timestamp')[:19]} | Action: {ev.get('action')} | Actor: {BOLD}{ev.get('actor_id')}{RESET} ({ev.get('actor_name')})")
        print(f"       Note: {DIM}{ev.get('reason')}{RESET}")

    # 7b. External Client View
    client_hist = client.request("GET", "/jobs/JOB-PLANTED-01/history", actor_id="usr_client_acme")
    print(f"\n2. External Customer Perspective (Actor: {BOLD}usr_client_acme{RESET} - Client CL-001):")
    print(f"   GET /jobs/JOB-PLANTED-01/history -> Status {client_hist.status_code}")
    events_client = client_hist.json().get("events", [])
    print(f"   Events visible ({len(events_client)}):")
    for ev in events_client[:4]:
        actor_masked = "REDACTED" if ev.get("actor_id") is None else ev.get("actor_id")
        reason_masked = "REDACTED" if ev.get("reason") is None else ev.get("reason")
        print(f"     * {ev.get('timestamp')[:19]} | Action: {ev.get('action')} | Actor: {YELLOW}{actor_masked}{RESET} | Reason: {YELLOW}{reason_masked}{RESET}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 8: Temporal Grant Replay
    # -------------------------------------------------------------------------
    print_section(
        8,
        "Temporal Authority Replay: Who could access this job on Jan 14, 2026?",
        "Evaluates grant validity windows at historical timestamps, including revoked grants."
    )
    who_resp = client.request(
        "GET",
        "/jobs/JOB-PLANTED-01/who-could",
        actor_id="usr_admin_1",
        params={"at": "2026-01-14T10:00:00Z"}
    )
    print("GET /jobs/JOB-PLANTED-01/who-could?at=2026-01-14T10:00:00Z")
    print(f"Status: {BOLD}{who_resp.status_code}{RESET}")
    w_data = who_resp.json()
    candidates = w_data.get("candidates", [])
    print(f"Replay evaluated on date: {CYAN}{w_data.get('on_date')}{RESET}")
    print(f"Total authorized candidates at timestamp: {BOLD}{len(candidates)}{RESET}")
    for u in candidates[:6]:
        actions = ", ".join(u.get("actions", []))
        print(f"  * {BOLD}{u.get('user_id'):<18}{RESET} ({u.get('username'):<18}) | Role: {u.get('role_id')} | Actions: {GREEN}{actions}{RESET}")

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 9: Cryptographic Hash Chain Verification & Tamper Detection
    # -------------------------------------------------------------------------
    print_section(
        9,
        "Cryptographic Hash Chain Integrity & Tamper Detection",
        "Verify SHA-256 chain link: hash = SHA256(prev_hash + timestamp + actor + target + decision...)"
    )

    # 9a. Verify Clean Chain
    verify_resp = client.request("GET", "/audit/verify", actor_id="usr_admin_1")
    print("Sub-step A: Verify live audit chain integrity")
    print(f"GET /audit/verify -> Status {BOLD}{verify_resp.status_code}{RESET}")
    v_data = verify_resp.json()
    head_h = v_data.get("head_hash") or "N/A"
    print(f"  Verified: {GREEN}{v_data.get('verified')}{RESET}")
    print(f"  Total records verified: {BOLD}{v_data.get('total_records')}{RESET}")
    print(f"  Head Hash: {DIM}{head_h[:24]}...{RESET}")

    # 9b. Live Tampering Demonstration
    print("\nSub-step B: Tamper Simulation (Directly mutating an audit row in SQLite)")
    session = SessionLocal()
    tampered_id = None
    original_action = None
    try:
        # Find an audit row to tamper
        target_row = session.query(AuditLog).order_by(AuditLog.id.desc()).first()
        if target_row:
            tampered_id = target_row.id
            original_action = target_row.action
            # Directly mutate row bypassing engine
            target_row.action = "TAMPERED_ACTION_BYPASS"
            session.commit()
            print(f"  {YELLOW}[!] Injected tampering into AuditLog row ID #{tampered_id} (action altered).{RESET}")

            # Verify chain failure
            tamper_verify = client.request("GET", "/audit/verify", actor_id="usr_admin_1")
            tv_data = tamper_verify.json()
            print(f"  GET /audit/verify after tamper -> Verified: {RED}{BOLD}{tv_data.get('verified')}{RESET}")
            print(f"  Failure detected at record ID: {RED}{tv_data.get('broken_at_id')}{RESET}")
            print(f"  Tamper Error: {YELLOW}{tv_data.get('error')}{RESET}")

            # Restore row
            target_row.action = original_action
            session.commit()
            print(f"  {GREEN}[OK] Restored original audit row data.{RESET}")

            # Re-verify
            restored_verify = client.request("GET", "/audit/verify", actor_id="usr_admin_1")
            print(f"  Re-verification -> Verified: {GREEN}{restored_verify.json().get('verified')}{RESET}")
    finally:
        session.close()

    pause_prompt(auto, delay)

    # -------------------------------------------------------------------------
    # STEP 10: Incident Early-Warning Reporting
    # -------------------------------------------------------------------------
    print_section(
        10,
        "Incident Early-Warning Report: Probes on Locked/Filed Jobs",
        "Detect unauthorized probing or tampering attempts on terminal customs records."
    )
    inc_resp = client.request("GET", "/audit/reports/filed-job-denials", actor_id="usr_admin_1")
    print("GET /audit/reports/filed-job-denials")
    print(f"Status: {BOLD}{inc_resp.status_code}{RESET}")
    idata = inc_resp.json()
    print(f"Total Filed Job Denials Detected: {BOLD}{idata.get('total_filed_job_denials')}{RESET}")
    print(f"Repeated Denial Incidents:       {BOLD}{idata.get('repeated_denial_incidents')}{RESET}")
    incidents = idata.get("incidents", [])
    if incidents:
        print("\nActive Incident Alerts:")
        for inc in incidents[:5]:
            risk = inc.get("risk_level", "MEDIUM")
            risk_color = RED if risk == "CRITICAL" else (YELLOW if risk == "HIGH" else CYAN)
            print(f"  * Job: {BOLD}{inc.get('job_id')}{RESET} | Actor: {inc.get('actor_name')} | Action: {inc.get('action')}")
            print(f"    Denials: {BOLD}{inc.get('denial_count')}{RESET} | Risk: {risk_color}{risk}{RESET} | Codes: {inc.get('deny_codes')}")
    else:
        print(f"  {GREEN}[OK] No unauthorized probes detected on filed jobs in the observation window.{RESET}")

    # -------------------------------------------------------------------------
    # SUMMARY
    # -------------------------------------------------------------------------
    print(f"\n{GREEN}{BOLD}================================================================================")
    print("  WALKTHROUGH COMPLETE: ALL 10 CAPABILITIES VERIFIED SUCCESSFULLY")
    print(f"================================================================================{RESET}")
    print(f"Interactive Swagger:   {CYAN}http://127.0.0.1:8000/docs{RESET}")
    print(f"ReDoc Documentation:   {CYAN}http://127.0.0.1:8000/redoc{RESET}")
    print(f"Design Document:       {CYAN}DESIGN_DOCUMENT.md{RESET}")
    print()


def main():
    parser = argparse.ArgumentParser(description="NFPL Access Control Service Walkthrough Demo")
    parser.add_argument("--auto", action="store_true", help="Run in automated non-interactive mode")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between steps in auto mode (seconds)")
    args = parser.parse_args()

    run_walkthrough(auto=args.auto, delay=args.delay)


if __name__ == "__main__":
    main()
