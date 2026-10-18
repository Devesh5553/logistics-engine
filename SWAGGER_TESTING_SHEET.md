# NFPL Access Control Service — Swagger UI Testing Cheatsheet

This testing sheet provides a complete, step-by-step guide to interactively test **every single endpoint** in the NFPL Access Control Service using the OpenAPI Swagger UI.

* **Swagger UI URL:** [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
* **ReDoc URL:** [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)
* **Service Status:** Ensure `python run.py serve` is running in your terminal.

---

## 🔑 Operational Personas & Test IDs Reference

Use these pre-seeded actor IDs in Swagger's `X-Actor-Id` header:

| User ID | Username | Role / Authority | Scope & Restrictions |
| :--- | :--- | :--- | :--- |
| `usr_admin_1` | `admin_primary` | **Admin** | National / Super-admin override, grant manager, approver |
| `usr_admin_2` | `admin_secondary` | **Admin** | Secondary operations admin |
| `usr_manager` | `staff_manager_del` | **Manager** | Delhi hub operations manager; dual-control approver |
| `usr_maker_del` | `staff_maker_del` | **Maker** | Delhi branch import/export shipments |
| `usr_checker_del` | `staff_checker_del` | **Checker** | Delhi branch compliance checker |
| `usr_maker_mum` | `staff_maker_mum` | **Maker** | Mumbai branch shipments only (out-of-scope for Delhi) |
| `usr_maker_wide` | `maker_wide_checker_few` | **Maker & Checker** | Holds Maker nationally + Checker for Client `CL-001` |
| `usr_filer_all` | `staff_filer_all` | **Filer** | National customs EDI/ICEGATE filing authority |
| `usr_client_acme` | `client_acme_user` | **Client User** | External customer restricted to Acme Corp (`CL-001`) |

### Key Pre-Seeded Job IDs
* `JOB-PLANTED-01`: Delhi Sea Import for Acme Corp (`CL-001`), Stage: `Confirmed`, Version: `1`
* `JOB-PLANTED-02`: Delhi Air Export for Acme Corp (`CL-001`), Stage: `Prepared`
* `JOB-PLANTED-03`: Mumbai Sea Import for Beta Logistics (`CL-002`)
* `JOB-PLANTED-08`: Filed Delhi Shipment (`is_locked = True`, stage: `Filed`)

---

## 📑 Complete Step-by-Step Endpoint Testing Guide

---

### GROUP 1: Health Check & Core Decision Engine

#### 1.1 `GET /health` — Service Liveness
* **Purpose:** Verify service health and database connectivity.
* **Swagger Location:** Expand `default` > `GET /health`
* **Click:** **Try it out** > **Execute**
* **Expected Status:** `200 OK`
* **Expected Response Body:**
  ```json
  {
    "status": "ok",
    "timestamp": "2026-...",
    "database": "connected"
  }
  ```

---

#### 1.2 `GET /can` — Positive Policy Check (Maker Allowed)
* **Purpose:** Check if Delhi Maker Rohan can prepare a Delhi import job.
* **Swagger Location:** Expand `default` > `GET /can`
* **Click:** **Try it out**
* **Parameters:**
  * `user_id`: `usr_maker_del`
  * `action`: `job:prepare`
  * `job_id`: `JOB-PLANTED-01`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Expected Response:** `"allowed": true`, `"reasons"` explaining grant match.

---

#### 1.3 `GET /can` — Denied Policy Check (Cross-Branch Block)
* **Purpose:** Verify Mumbai Maker Kavita is denied on Delhi job.
* **Parameters:**
  * `user_id`: `usr_maker_mum`
  * `action`: `job:prepare`
  * `job_id`: `JOB-PLANTED-01`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Expected Response:** `"allowed": false`, `"deny_code": "OUT_OF_SCOPE"`.

---

#### 1.4 `GET /compare` — Differential Access Explanation
* **Purpose:** Plain-English explanation of why Rohan can prepare while Kavita cannot.
* **Swagger Location:** Expand `default` > `GET /compare`
* **Click:** **Try it out**
* **Parameters:**
  * `user_a`: `usr_maker_del`
  * `user_b`: `usr_maker_mum`
  * `action`: `job:prepare`
  * `job_id`: `JOB-PLANTED-01`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Expected Response:**
  * `evaluation_a.allowed`: `true`
  * `evaluation_b.allowed`: `false`
  * `differential_reasons`: Explains branch mismatch (`DEL` vs `BOM`).

---

### GROUP 2: Actionable Work Queue

#### 2.1 `GET /users/{user_id}/queue` — Maker Queue
* **Purpose:** View actionable tasks for Maker Rohan.
* **Swagger Location:** Expand `default` > `GET /users/{user_id}/queue`
* **Click:** **Try it out**
* **Parameters:**
  * `user_id`: `usr_maker_del`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Expected Response:** List of `Confirmed` Delhi jobs where `action == "job:prepare"`.

---

#### 2.2 `GET /users/{user_id}/queue` — Checker Queue
* **Purpose:** View actionable tasks for Checker Pooja.
* **Parameters:**
  * `user_id`: `usr_checker_del`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Expected Response:** List of `Prepared` Delhi jobs where `action == "job:check"`.

---

#### 2.3 `GET /queue` — Fallback Query Queue Endpoint
* **Swagger Location:** Expand `default` > `GET /queue`
* **Parameters:**
  * `user_id`: `usr_maker_del`
* **Click:** **Execute**
* **Expected Status:** `200 OK` (returns identical actionable queue).

---

### GROUP 3: Job Scope & Operational Assignment

#### 3.1 `GET /jobs/{job_id}` — View Job Details
* **Swagger Location:** Expand `default` > `GET /jobs/{job_id}`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_maker_del`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Click:** **Execute**
* **Expected Status:** `200 OK` (returns stage, client, branch, version, goods description).

---

#### 3.2 `GET /jobs/{job_id}/assignments` — View Job Assignments
* **Swagger Location:** Expand `default` > `GET /jobs/{job_id}/assignments`
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Click:** **Execute**
* **Expected Status:** `200 OK` (returns active makers/checkers assigned).

---

#### 3.3 `POST /jobs/{job_id}/assignments` — Assign Operational Staff
* **Swagger Location:** Expand `default` > `POST /jobs/{job_id}/assignments`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_manager`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Request Body (JSON):**
  ```json
  {
    "assignee_id": "usr_maker_del",
    "role": "Maker"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK` (assignment created, audit record chained).

---

### GROUP 4: Workflow Progression & State Machine

#### 4.1 `POST /jobs/{job_id}/transition` — Prepare Job (Step 1)
* **Purpose:** Advance `JOB-PLANTED-01` from `Confirmed` to `Prepared`.
* **Swagger Location:** Expand `default` > `POST /jobs/{job_id}/transition`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_maker_wide`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Request Body (JSON):**
  ```json
  {
    "action": "job:prepare",
    "version": 1,
    "reason": "Clearance documents verified"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify Response:** `"stage": "Prepared"`, `"version": 2`, `"prepared_by": "usr_maker_wide"`.

---

#### 4.2 `POST /jobs/{job_id}/transition` — Self-Approval Block (Segregation of Duty)
* **Purpose:** Prove that Deepa (`usr_maker_wide`) CANNOT check the file she prepared.
* **Header `X-Actor-Id`:** `usr_maker_wide`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Request Body (JSON):**
  ```json
  {
    "action": "job:check",
    "version": 2,
    "reason": "Attempting self-check"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `403 Forbidden`
* **Verify Response:**
  ```json
  {
    "detail": {
      "deny_code": "MAKER_CHECKER_CONFLICT",
      "reasons": [
        "Separation of duties violation: user prepared this shipment checklist and cannot check or approve their own work."
      ],
      "action": "job:check"
    }
  }
  ```

---

#### 4.3 `POST /jobs/{job_id}/transition` — Independent Secondary Review (Step 2)
* **Purpose:** Pooja Gupta (`usr_checker_del`) checks the file.
* **Header `X-Actor-Id`:** `usr_checker_del`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Request Body (JSON):**
  ```json
  {
    "action": "job:check",
    "version": 2,
    "reason": "Independent checklist verified and compliant"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify Response:** `"stage": "Checked"`, `"version": 3`, `"checked_by": "usr_checker_del"`.

---

#### 4.4 `POST /jobs/{job_id}/transition` — File to ICEGATE (Step 3)
* **Purpose:** Suresh (`usr_filer_all`) files the declaration to Indian Customs.
* **Header `X-Actor-Id`:** `usr_filer_all`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Request Body (JSON):**
  ```json
  {
    "action": "job:file",
    "version": 3,
    "reason": "Electronic Bill of Entry transmitted to ICEGATE"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify Response:** `"stage": "Filed"`, `"version": 4`, `"locked": true`.

---

### GROUP 5: Dual-Control High-Risk Approvals

#### 5.1 Direct High-Risk Cancellation (Blocked)
* **Purpose:** Prove direct cancellation of a filed job is blocked.
* **Header `X-Actor-Id`:** `usr_maker_del`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Request Body (JSON):**
  ```json
  {
    "action": "job:cancel_filed",
    "version": 4,
    "reason": "Commercial dispute"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `403 Forbidden`
* **Verify Response:** `"deny_code": "APPROVAL_REQUIRED"`.

---

#### 5.2 `POST /approvals` — Create Dual-Control Ticket
* **Purpose:** Submit an approval request for high-risk cancellation.
* **Swagger Location:** Expand `default` > `POST /approvals`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_maker_del`
* **Request Body (JSON):**
  ```json
  {
    "action": "job:cancel_filed",
    "target_type": "job",
    "target_id": "JOB-PLANTED-01",
    "reason": "Importer requested commercial cancellation prior to cargo inspection"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Important:** Copy the generated `"id"` (e.g. `appr_...`).

---

#### 5.3 `POST /approvals/{approval_id}/approve` — Self-Authorization Block
* **Purpose:** Requester attempts to approve their own ticket.
* **Swagger Location:** Expand `default` > `POST /approvals/{approval_id}/approve`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_maker_del`
* **Parameter `approval_id`:** *(Paste `appr_...` ID from Step 5.2)*
* **Click:** **Execute**
* **Expected Status:** `403 Forbidden`
* **Verify Response:** `"deny_code": "SELF_APPROVAL_PROHIBITED"`.

---

#### 5.4 `POST /approvals/{approval_id}/approve` — Authorize as Admin
* **Purpose:** Operations Admin Anand authorizes the ticket.
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `approval_id`:** *(Paste `appr_...` ID)*
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify Response:** `"status": "approved"`, `"approved_by": "usr_admin_1"`.

---

#### 5.5 `GET /approvals/{approval_id}` — Inspect Approval Ticket
* **Swagger Location:** Expand `default` > `GET /approvals/{approval_id}`
* **Parameter `approval_id`:** *(Paste `appr_...` ID)*
* **Click:** **Execute**
* **Expected Status:** `200 OK` (`status: approved`, `used: false`).

---

#### 5.6 `POST /jobs/{job_id}/transition` — Execute Cancellation with Approval Token
* **Purpose:** Consume the single-use approval token.
* **Header `X-Actor-Id`:** `usr_maker_del`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Request Body (JSON):**
  ```json
  {
    "action": "job:cancel_filed",
    "version": 4,
    "approval_id": "<PASTE_APPR_ID_HERE>",
    "reason": "Commercial dispute approved by admin"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify Response:** `"stage": "Cancelled"`, `"version": 5`.

---

#### 5.7 Token Replay Block (Single-Use Enforcement)
* **Purpose:** Re-execute the exact same request from 5.6 with the same `approval_id`.
* **Click:** **Execute** again
* **Expected Status:** `400 Bad Request` or `403 Forbidden`
* **Verify Response:** Replay rejected because the approval token is already marked `used: true`.

---

#### 5.8 `POST /approvals/{approval_id}/reject` — Reject Ticket
* **Purpose:** Test rejecting an approval request.
* **Create a new ticket** via 5.2, then call:
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `approval_id`:** *(New `appr_...` ID)*
* **Click:** **Execute**
* **Expected Status:** `200 OK` (`status: rejected`).

---

### GROUP 6: Administration & Management Scope Changes

#### 6.1 `POST /jobs/{job_id}/override` — Super-Admin Emergency Override
* **Swagger Location:** Expand `default` > `POST /jobs/{job_id}/override`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `job_id`:** `JOB-PLANTED-02`
* **Request Body (JSON):**
  ```json
  {
    "action": "admin:override",
    "reason": "Emergency customs officer request to revert stage",
    "target_stage": "Confirmed",
    "unlock": true
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK` (stage forced to `Confirmed`, locked reset).

---

#### 6.2 `PATCH /jobs/{job_id}/scope` (or `POST /jobs/{job_id}/scope`) — Scope Correction
* **Swagger Location:** Expand `default` > `PATCH /jobs/{job_id}/scope`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_manager`
* **Parameter `job_id`:** `JOB-PLANTED-02`
* **Request Body (JSON):**
  ```json
  {
    "reason": "Manifest cargo routing redirected to Mumbai hub",
    "branch_id": "BOM"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK` (`branch_id` updated to `BOM`, audit row recorded).

---

### GROUP 7: Grant Management & Delegation Lifecycle

#### 7.1 `POST /grants` — Create Direct Grant
* **Swagger Location:** Expand `default` > `POST /grants`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Request Body (JSON):**
  ```json
  {
    "user_id": "usr_maker_del",
    "role_id": "role_checker",
    "branch_id": "DEL",
    "source": "direct",
    "reason": "Promotion to senior checker"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK` (grant created, copy `id`).

---

#### 7.2 `PATCH /grants/{grant_id}` — Shorten Grant Validity
* **Swagger Location:** Expand `default` > `PATCH /grants/{grant_id}`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `grant_id`:** *(Paste grant ID from 7.1)*
* **Request Body (JSON):**
  ```json
  {
    "valid_to": "2026-12-31T23:59:59Z",
    "reason": "Temporary assignment end date adjusted"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK`.

---

#### 7.3 `DELETE /grants/{grant_id}` (or `POST /grants/{grant_id}/revoke`) — Revoke Grant
* **Swagger Location:** Expand `default` > `DELETE /grants/{grant_id}`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `grant_id`:** *(Paste grant ID from 7.1)*
* **Request Body (JSON):**
  ```json
  {
    "reason": "Role revoked following organizational restructure"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK` (`revoked_at` populated, child delegations cascaded).

---

### GROUP 8: Personnel Life-Cycle (Leaver & Transfer)

#### 8.1 `POST /users/{user_id}/deactivate` — Employee Departure
* **Swagger Location:** Expand `default` > `POST /users/{user_id}/deactivate`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `user_id`:** `usr_maker_mum`
* **Request Body (JSON):**
  ```json
  {
    "reason": "Employee resigned from company"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK` (`is_active: false`, all assigned jobs flagged for reassignment).

---

#### 8.2 `POST /users/{user_id}/transfer` — Branch Transfer
* **Swagger Location:** Expand `default` > `POST /users/{user_id}/transfer`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `user_id`:** `usr_maker_del`
* **Request Body (JSON):**
  ```json
  {
    "from_branch_id": "DEL",
    "to_branch_id": "MAA",
    "reason": "Relocated to Chennai port operations"
  }
  ```
* **Click:** **Execute**
* **Expected Status:** `200 OK`.

---

### GROUP 9: Multi-Tenant Privacy & Temporal Historical Replay

#### 9.1 `GET /jobs/{job_id}/history` — Internal Staff View
* **Swagger Location:** Expand `default` > `GET /jobs/{job_id}/history`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify:** Full employee usernames, actor IDs, and internal operational notes are visible.

---

#### 9.2 `GET /jobs/{job_id}/history` — External Customer View (Privacy Redaction)
* **Header `X-Actor-Id`:** `usr_client_acme` (External Client Acme)
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify:**
  * `actor_id` displays as `"REDACTED"`
  * `reason` displays as `"REDACTED"`
  * Lifecycle stages (`Confirmed`, `Prepared`, etc.) remain clean and visible.

---

#### 9.3 `GET /jobs/{job_id}` — Silent 404 on Competitor Job Probe
* **Swagger Location:** Expand `default` > `GET /jobs/{job_id}`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_client_acme`
* **Parameter `job_id`:** `JOB-PLANTED-03` *(Belongs to competitor Client Beta)*
* **Click:** **Execute**
* **Expected Status:** `404 Not Found` (Not 403, preventing cross-tenant enumeration).

---

#### 9.4 `GET /jobs/{job_id}/who-could` — Temporal Historical Replay
* **Purpose:** Query which users had authority on a past date.
* **Swagger Location:** Expand `default` > `GET /jobs/{job_id}/who-could`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameter `job_id`:** `JOB-PLANTED-01`
* **Parameter `at`:** `2026-01-14T10:00:00Z`
* **Click:** **Execute**
* **Expected Status:** `200 OK`
* **Verify:** Returns list of 14 authorized candidates evaluated against Jan 14, 2026 validity windows.

---

### GROUP 10: Cryptographic Audit Trail & Incident Detection

#### 10.1 `GET /audit` — Query Audit Trail
* **Swagger Location:** Expand `default` > `GET /audit`
* **Click:** **Try it out**
* **Header `X-Actor-Id`:** `usr_admin_1`
* **Parameters:** `limit: 10`
* **Click:** **Execute**
* **Expected Status:** `200 OK` (returns recent chained audit records).

---

#### 10.2 `GET /audit/verify` — SHA-256 Hash Chain Integrity Verification
* **Purpose:** Verify cryptographic integrity of every record from genesis to head.
* **Swagger Location:** Expand `default` > `GET /audit/verify`
* **Click:** **Try it out** > **Execute**
* **Expected Status:** `200 OK`
* **Expected Response:**
  ```json
  {
    "verified": true,
    "records_checked": 45,
    "broken_record_id": null,
    "head_hash": "...",
    "genesis_hash": "0000000000000000000000000000000000000000000000000000000000000000"
  }
  ```

---

#### 10.3 `GET /audit/reports/filed-job-denials` — Locked Job Incident Alert Monitor
* **Purpose:** Early-warning report detecting unauthorized probing or tampering on locked/filed jobs.
* **Swagger Location:** Expand `default` > `GET /audit/reports/filed-job-denials`
* **Click:** **Try it out** > **Execute**
* **Expected Status:** `200 OK`
* **Expected Response:**
  * `total_filed_job_denials`: Count of intrusion/probe attempts on locked files
  * `repeated_denial_incidents`: High-risk repeated attempts
  * `incidents`: Granular list of targeted jobs, actors, and deny codes.

---

## ⚡ Quick Troubleshooting Tips in Swagger UI

1. **`401 Unauthorized`**: You forgot to enter `X-Actor-Id` in the header field. Fill it in with `usr_admin_1` or another persona.
2. **`409 Conflict` (Version Mismatch)**: The job was already transitioned and its version incremented. Check current version with `GET /jobs/{job_id}` and increment the `version` field in your payload.
3. **Resetting Database**: If you want to start fresh at any time, run:
   ```bash
   python run.py seed
   ```
   This resets all jobs, grants, and audit chains back to the initial baseline!
