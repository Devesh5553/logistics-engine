# Nagarkot Forwarders Pvt. Ltd. (NFPL) - Access Control Service

A scoped-role, explainable, audit-first access control system designed for customs brokerage operations. Built with Python 3.12+, FastAPI, and SQLAlchemy 2.0.

## Overview
NFPL operates across 5 branches handling customs clearance jobs across diverse clients, directions (Import/Export), and transport modes (Air, FCL, LCL). This service decides **who may do what on which job**, explains every decision, shows staff jobs needing their action, and maintains an append-only, tamper-evident hash-chained audit trail.

---

## Quickstart

### Prerequisites
- Python 3.12+ (Tested on Python 3.13)
- Dependencies installed:
  ```bash
  pip install -r requirements.txt
  ```

### 1. Initialize and Seed Small Deliberate Dataset
```bash
python run.py seed
```
*Or via make:*
```bash
make seed
```
This initializes the database schema and loads:
- **5 Branches**: Standard 3-letter IATA hub codes (`DEL`, `MUM`, `KOL`, `MAA`, `BLR`)
- **10 Clients**: Global and domestic enterprises (`CL-001` through `CL-010`)
- **22 Users**: Deliberately configured test personas covering dual admins, branch manager, wildcard Maker, narrow Import Checker, mode-restricted Air Checker, absent user with 7-day delegation, revoked worker, dual leavers, client-side read-only users, and automated EDI parser.
- **300 Jobs**: 15 fixed planted edge-case jobs + 285 seeded operational jobs across all stages

### 2. Generate and Verify Scale Case (500 Users, 200,000 Jobs)
```bash
python run.py seed-scale
```
*Or via make:*
```bash
make seed-scale
```
> [!NOTE]
> **Database Isolation:** `seed-scale` operates strictly on a dedicated database file (`data/access_control_scale.db`). The small deliberate dataset (`data/access_control.db`) is **never overwritten**, guaranteeing that unit and integration tests remain isolated and deterministic.

### 3. Run Automated Tests
```bash
python -m pytest -v
```
*Or via make:*
```bash
make test
```

### 4. Launch Service & Interactive Swagger Docs
```bash
python run.py serve
```
*Or via make:*
```bash
make serve
```
- **Interactive Swagger Documentation**: **http://127.0.0.1:8000/docs**
- **ReDoc Documentation**: **http://127.0.0.1:8000/redoc**
- Health Check: `GET /health`
- Decision Engine: `GET /can?user_id=...&action=...&job_id=...`
- Differential Comparison: `GET /compare?user_a=...&user_b=...&action=...&job_id=...`

### 5. Run 5-Minute Automated / Interactive Demo Walkthrough
A dedicated terminal walkthrough demonstrates all core capabilities end-to-end:
```bash
# Automated mode (runs all 10 core capability steps)
python demo_walkthrough.py --auto

# Interactive mode (step-by-step with [Enter])
python demo_walkthrough.py
```
**Demonstrates:**
1. Service health and planted operational personas overview.
2. Differential access comparison ("Why can Rohan do this and Kavita can't?").
3. Actionable work queue for active makers/checkers (`/users/{id}/queue`).
4. Workflow state machine progression (`PREPARE`, `CHECK`, `FILE`, and dual-control self-approval block).
5. High-risk action blocking (Direct cancellation of filed job blocked).
6. Dual-control approval request lifecycle (request, 2nd approver approve, single-use execution, replay block).
7. Client privacy boundary (Internal staff audit timeline vs external client redaction).
8. Temporal grant replay (Reconstructing who had authority at past dates).
9. Cryptographic hash chain verification and live tamper detection (`/audit/verify`).
10. Incident early-warning reporting (Detecting repeated probes on filed jobs).

### 6. Architectural Design Document (`DESIGN_DOCUMENT.md`)
A comprehensive architectural specification is provided in [DESIGN_DOCUMENT.md](file:///c:/Users/Devesh/OneDrive/Documents/nagarkot/DESIGN_DOCUMENT.md), covering:
- **Core Model & Trade-offs**: Hierarchical Scoped-Role Model vs pure ReBAC/ABAC, fail-closed policy.
- **Architectural Questions Q1–Q19**:
  - `Q1`: Model evaluation and comparison.
  - `Q2`: Scoped-role hierarchy and wildcard inheritance.
  - `Q3`: Temporal validity windows and delegation cascading.
  - `Q4`: Fine-grained action permissions.
  - `Q5`: Multi-branch and client scoping mechanics.
  - `Q6`: State machine workflow transitions and guards.
  - `Q7`: Dual-control high-risk operations.
  - `Q8`: Maker-checker self-approval separation of duties.
  - `Q9`: Multi-tenant data isolation and client privacy redaction.
  - `Q10`: Append-only audit logging and SHA-256 hash chaining.
  - `Q11`: Explainability engine and differential comparisons.
  - `Q12`: Actionable queue aggregation and composite indexing (<50ms at 200k jobs).
  - `Q13`: Early-warning incident reporting and pattern alerts.
  - `Q14`: Emergency access, administrative overrides, and dual authorization.
  - `Q15`: Operational scale benchmark and query plan verification.
  - `Q16`: Cryptographic tamper detection mechanics.
  - `Q17`: Single-use approval tokens and replay defense.
  - `Q18`: Leaver departures, branch transfers, and cascade reassignments.
  - `Q19`: Tenant isolation, side-channel leak mitigation, and silent 404s.

---

## Standard Reference Data & Conventions

### Branch Codes Convention
All system entities use standard **3-letter IATA / Hub Airport Codes** for consistency across database records, API routes, and customs EDI feeds:
- `DEL`: Delhi Northern Inland Hub / IGI Airport
- `MUM`: Mumbai Port & Air Cargo Complex
- `KOL`: Kolkata Eastern Sea & Air Port
- `MAA`: Chennai Southern Coastal Gateway
- `BLR`: Bengaluru Technology Depot / Devanahalli Cargo Hub

<!-- BRANCHES_TABLE_START -->
| Branch Code | Hub Name | City | Standard Convention |
| :--- | :--- | :--- | :--- |
| `BLR` | Bengaluru Tech Depot | Bengaluru | 3-letter IATA / Hub Code |
| `DEL` | Delhi Northern Hub | New Delhi | 3-letter IATA / Hub Code |
| `KOL` | Kolkata Eastern Port | Kolkata | 3-letter IATA / Hub Code |
| `MAA` | Chennai Southern Hub | Chennai | 3-letter IATA / Hub Code |
| `MUM` | Mumbai Port & Air Cargo | Mumbai | 3-letter IATA / Hub Code |
<!-- BRANCHES_TABLE_END -->

### Client Profiles Reference
<!-- CLIENTS_TABLE_START -->
| Client ID | Client Enterprise Name | Industry | Status |
| :--- | :--- | :--- | :--- |
| `CL-001` | **Acme Global Trading** | Consumer Goods | Active |
| `CL-002` | **Zenith Electronics** | High-Tech | Active |
| `CL-003` | **Bharat Auto Components** | Automotive | Active |
| `CL-004` | **Himalaya Pharmaceuticals** | Healthcare | Active |
| `CL-005` | **Orient Textiles** | Apparel | Active |
| `CL-006` | **Pacific Solar Energy** | Renewables | Active |
| `CL-007` | **Indus Chemicals** | Specialty Chemicals | Active |
| `CL-008` | **Deccan Heavy Machinery** | Industrial | Active |
| `CL-009` | **Royal Gems & Jewellery** | Luxury | Active |
| `CL-010` | **Sunrise Agro Exports** | Agriculture | Active |
<!-- CLIENTS_TABLE_END -->

---

## Role Permissions & Stage Guardrails
Roles and permissions are stored directly in the database as configuration data (satisfying Q12). High-risk actions enforce separation of duties, mandatory reasons, and dual-control second approver sign-offs.

<!-- ROLE_PERMISSIONS_START -->
| Role | Allowed Actions / Permissions | Stage Limits & Guardrails | Description |
| :--- | :--- | :--- | :--- |
| **Admin** | `admin:override`, `audit:read`, `audit:verify`, `grant:manage`, `job:assign`, `job:edit_scope`, `job:view`, `job:view_internal`, `user:manage` | `job:assign` limited to Confirmed, Prepared, Checked, Reopened; `job:edit_scope` requires 2nd approver + mandatory reason | System configuration, user provisioning, and audit oversight |
| **Auditor** | `audit:read`, `audit:verify`, `job:view`, `job:view_internal` | Standard stage workflow | Read-only inspection of operations and cryptographic audit verification |
| **Checker** | `job:check`, `job:view`, `job:view_internal` | `job:check` limited to Prepared -> Checked | Verifies prepared customs documentation (Separation of duties applies) |
| **ClientUser** | `job:view` | **Strict:** Read-only for own client. Internal notes & other clients hidden. | External customer tracking: isolated strictly to customer's own jobs |
| **Filer** | `job:file`, `job:view`, `job:view_internal` | `job:file` limited to Checked -> Filed | Submits verified declarations to customs portal ICEGATE |
| **Maker** | `job:create`, `job:prepare`, `job:view`, `job:view_internal` | `job:create` limited to Confirmed; `job:prepare` limited to Confirmed -> Prepared | Prepares customs entries and shipping bills from Confirmed stage |
| **Manager** | `job:assign`, `job:cancel_filed`, `job:edit_scope`, `job:reopen_closed`, `job:view`, `job:view_internal` | `job:assign` limited to Confirmed, Prepared, Checked, Reopened; `job:cancel_filed` requires 2nd approver + mandatory reason; `job:edit_scope` requires 2nd approver + mandatory reason; `job:reopen_closed` requires 2nd approver + mandatory reason | High-risk authority: dual-control cancellation, reopening, scope changes |
| **Parser** | `job:create`, `job:prepare`, `job:view` | **Strict:** Create/Prepare at Confirmed/Prepared only. CANNOT file, cancel, or reopen. | Automated EDI parser: limited to Confirmed and Prepared stages only |
| **Viewer** | `job:view`, `job:view_internal` | Standard stage workflow | General internal visibility without operational execution rights |
<!-- ROLE_PERMISSIONS_END -->

---

## Deliberate Planted Test Jobs (15 Fixed Cases)
The dataset includes 15 permanently anchored jobs engineered to prove every security boundary, Maker-Checker constraint, and delegation flow:

<!-- PLANTED_JOBS_START -->
| Job ID | Client | Branch | Dir | Mode | Stage | Prepared By | Test Case / Policy Tested |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `JOB-PLANTED-01` | `CL-001` (Acme Global Trading) | `DEL` | `IMPORT` | `AIR` | **Confirmed** | - | Air import under CL-001; allowed for maker_wide, checker_narrow, and checker_air_only |
| `JOB-PLANTED-02` | `CL-001` (Acme Global Trading) | `DEL` | `EXPORT` | `AIR` | **Confirmed** | - | Air export under CL-001; tests direction restriction (checker_narrow denied on EXPORT) |
| `JOB-PLANTED-03` | `CL-001` (Acme Global Trading) | `MUM` | `IMPORT` | `AIR` | **Confirmed** | - | Mumbai job; tests branch boundary (Delhi-scoped staff denied in Mumbai) |
| `JOB-PLANTED-04` | `CL-003` (Bharat Auto Components) | `DEL` | `IMPORT` | `AIR` | **Confirmed** | - | CL-003 job; tests client restriction (maker_wide can prepare, but cannot check) |
| `JOB-PLANTED-05` | `CL-001` (Acme Global Trading) | `DEL` | `IMPORT` | `AIR` | **Prepared** | maker_wide_checker_few | Prepared by Rohan; tests Maker-Checker conflict (preparer denied check despite qualification) |
| `JOB-PLANTED-06` | `CL-001` (Acme Global Trading) | `DEL` | `IMPORT` | `FCL` | **Confirmed** | - | Ocean FCL import for CL-001; tests mode restriction (checker_air_only denied on non-AIR mode) |
| `JOB-PLANTED-07` | `CL-001` (Acme Global Trading) | `DEL` | `IMPORT` | `FCL` | **Checked** | staff_maker_del | Customs checklist verified; ready for final filing by staff_filer_all |
| `JOB-PLANTED-08` | `CL-001` (Acme Global Trading) | `DEL` | `IMPORT` | `FCL` | **Filed** | staff_maker_del | Filed customs bill for CL-001; tests Manager dual-control cancellation and Acme client view |
| `JOB-PLANTED-09` | `CL-002` (Zenith Electronics) | `MUM` | `EXPORT` | `LCL` | **Filed** | staff_maker_mum | Filed export bill for CL-002; tests client isolation (Zenith views, Acme denied) and parser lock |
| `JOB-PLANTED-10` | `CL-004` (Himalaya Pharmaceuticals) | `DEL` | `EXPORT` | `AIR` | **Cancelled** | - | Cancelled job; tests dual-control reopen flow requiring Manager approval |
| `JOB-PLANTED-11` | `CL-005` (Orient Textiles) | `KOL` | `IMPORT` | `FCL` | **Reopened** | staff_maker_all | Reopened job in-flight under approved amendment APPR-001 |
| `JOB-PLANTED-12` | `CL-006` (Pacific Solar Energy) | `BLR` | `IMPORT` | `AIR` | **Prepared** | user_absent | Prepared by Vikram; tests absence handling and routing to delegate Sunita |
| `JOB-PLANTED-13` | `CL-007` (Indus Chemicals) | `MAA` | `EXPORT` | `FCL` | **Prepared** | user_revoked | In-flight job handled by Anand; tests changed-responsibility and reassignment flagging |
| `JOB-PLANTED-14` | `CL-008` (Deccan Heavy Machinery) | `MUM` | `IMPORT` | `FCL` | **Prepared** | user_leaver | In-flight job handled by leaver Dev Patel; tests leaver deactivation and reassignment |
| `JOB-PLANTED-15` | `CL-001` (Acme Global Trading) | `DEL` | `IMPORT` | `LCL` | **Confirmed** | - | Early-stage import from EDI; tests automated parser creation/update at Confirmed stage |
<!-- PLANTED_JOBS_END -->

---

## Plain-English User Access Matrix
Generated directly from active database grants. Grouped by person with operational statuses and scopes clearly displayed:

<!-- ACCESS_LINES_START -->
- **John Doe (Acme Logistics Manager)** (`client_acme_user`) [Active]
  - Role **ClientUser** for client *CL-001 (Acme Global Trading)*, branch *all branches*, direction *all directions*, mode *all modes*
- **Li Wei (Zenith Supply Chain Lead)** (`client_zenith_user`) [Active]
  - Role **ClientUser** for client *CL-002 (Zenith Electronics)*, branch *all branches*, direction *all directions*, mode *all modes*
- **AutoEDIPayloadParser (Nightly Cron Job)** (`service_parser`) [Active]
  - Role **Parser** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Aarav Sharma (Head of Compliance)** (`admin_primary`) [Active]
  - Role **Admin** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Priya Nair (Operations Director)** (`admin_secondary`) [Active]
  - Role **Admin** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Sanjay Singhania (Air Freight Specialist)** (`checker_air_only`) [Active]
  - Role **Checker** for client *CL-001 (Acme Global Trading)*, branch *DEL*, direction *all directions*, mode *AIR*
- **Kavita Rao (Delhi Import Checker Specialist)** (`checker_narrow`) [Active]
  - Role **Checker** for client *CL-001 (Acme Global Trading)*, branch *DEL*, direction *IMPORT*, mode *all modes*
- **Rohan Mehta (Senior Maker / Occasional Checker)** (`maker_wide_checker_few`) [Active]
  - Role **Maker** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
  - Role **Checker** for client *CL-001 (Acme Global Trading)*, branch *all branches*, direction *all directions*, mode *all modes*
  - Role **Checker** for client *CL-002 (Zenith Electronics)*, branch *all branches*, direction *all directions*, mode *all modes*
- **Divya Sen (Internal Risk & Audit Officer)** (`staff_auditor`) [Active]
  - Role **Auditor** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Meera Iyer (National Senior Checker)** (`staff_checker_all`) [Active]
  - Role **Checker** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Rajesh Pillai (Mumbai Lead Checker)** (`staff_checker_mum`) [Active]
  - Role **Checker** for client *all clients*, branch *MUM*, direction *all directions*, mode *all modes*
- **Suresh Menon (Authorized Customs Broker Filer)** (`staff_filer_all`) [Active]
  - Role **Filer** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Deepak Chopra (National Maker)** (`staff_maker_all`) [Active]
  - Role **Maker** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Amit Kumar (Delhi Customs Preparer)** (`staff_maker_del`) [Active]
  - Role **Maker** for client *all clients*, branch *DEL*, direction *all directions*, mode *all modes*
- **Neha Joshi (Mumbai Port Specialist)** (`staff_maker_mum`) [Active]
  - Role **Maker** for client *all clients*, branch *MUM*, direction *all directions*, mode *all modes*
- **Vikrant Deshmukh (Branch Operations Manager)** (`staff_manager_del`) [Active]
  - Role **Manager** for client *all clients*, branch *DEL*, direction *all directions*, mode *all modes*
- **Pooja Gupta (Junior Trainee)** (`staff_trainee`) [Active]
  - Role **Viewer** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes*
- **Vikram Malhotra (On Medical Leave)** (`user_absent`) [ABSENT: 2026-01-13 to 2026-01-22 (Medical leave following surgery)]
  - Role **Maker** for client *all clients*, branch *DEL*, direction *all directions*, mode *all modes*
  - Role **Checker** for client *all clients*, branch *DEL*, direction *all directions*, mode *all modes*
- **Sunita Verma (Designated Cover for Vikram)** (`user_delegate`) [Active]
  - Role **Checker** for client *all clients*, branch *DEL*, direction *all directions*, mode *all modes* (delegated from absent colleague Vikram Malhotra; valid from 2026-01-15 to 2026-01-22, valid for 7 days from seeding)
- **Dev Patel (Former Operations Officer)** (`user_leaver`) [DEACTIVATED on 2026-01-05]
  - Role **Maker** for client *all clients*, branch *MUM*, direction *all directions*, mode *all modes* (REVOKED: Left the company)
- **Karan Kapoor (Deactivated Staff with Unrevoked Grants)** (`user_leaver_active_grants`) [DEACTIVATED on 2026-01-10]
  - Role **Maker** for client *all clients*, branch *MUM*, direction *all directions*, mode *all modes* (Active grant unrevoked: isolates inactive user check)
- **Anand Verma (Transferred to Accounts)** (`user_revoked`) [Active]
  - Role **Checker** for client *all clients*, branch *all branches*, direction *all directions*, mode *all modes* (REVOKED: Transferred to non-customs department)
<!-- ACCESS_LINES_END -->

---

## Scale Performance Benchmark Evidence

The scale case generates **500 users** and **200,000 jobs** across all 5 branches, 25 clients, and all stages. Query performance is guaranteed by SQLite composite indexes:
- `ix_jobs_scope_stage`: `(client_id, branch_id, direction, mode, stage)`
- `ix_jobs_branch_stage`: `(branch_id, stage)`
- `ix_jobs_stage`: `(stage)`
- `ix_grants_user_validity`: `(user_id, valid_to, revoked_at, valid_from)`

Full benchmark execution report is generated automatically by `python run.py seed-scale` and saved to `scale_report.txt`.

### Measured Latency Numbers (100 Iterations across 200,000 Jobs)
*Generated on Windows 11, Intel Core i7, Python 3.13.1, SQLite 3.45.3 (see `scale_report.txt` for machine environment).*

| Query Scenario | Average Latency | p95 Latency | Worst-Case (Max) | Target Constraint | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Exact Composite Scope Match** (`CL-001`, `DEL`, `IMPORT`, `AIR`, `Confirmed`) | **0.76 ms** | **2.13 ms** | **4.24 ms** | < 50 ms | **PASS** |
| **Branch Stage Queue Aggregation** (`DEL` branch, `Confirmed` & `Prepared`) | **6.12 ms** | **8.88 ms** | **13.51 ms** | < 50 ms | **PASS** |
| **Wildcard Scope Queue Query** (National overview by `stage`) | **0.64 ms** | **2.50 ms** | **4.02 ms** | < 50 ms | **PASS** |
| **Live Uncached Grant Lookup** (Direct grant evaluation per request) | **0.34 ms** | **0.99 ms** | **2.94 ms** | < 10 ms | **PASS** |

> [!NOTE]
> **Performance Analysis & Production Hardening:**
> - All queue and scope queries consistently execute between **0.34 ms and 13.51 ms** across 100 iterations on 200,000 jobs (well below the 50 ms constraint).
> - Index usage was verified via `EXPLAIN QUERY PLAN` confirming index seeks and covering index scans across all four test patterns.
> - **Production Note:** In PostgreSQL (the target production engine), declarative table partitioning on `branch_id` combined with covering indexes (`INCLUDE (stage)`) guarantees low latencies even under millions of records.

### Verified Query Plan (`EXPLAIN QUERY PLAN`)
- **Narrow Scope Match:** Uses index `ix_jobs_scope_stage` covering `client_id, branch_id, direction, mode, stage` (`SEARCH jobs USING INDEX ix_jobs_scope_stage`).
- **Branch Queue Aggregation:** Uses index `ix_jobs_branch_stage` covering `branch_id, stage` (`SEARCH jobs USING COVERING INDEX ix_jobs_branch_stage`).
- **Wildcard Queue Query:** Uses index `ix_jobs_stage` covering `stage` (`SEARCH jobs USING INDEX ix_jobs_stage`).
- **Live Grant Resolution:** Uses index `ix_grants_user_validity` (`SEARCH grants USING INDEX ix_grants_user_validity`).

---

## Known limits and cuts

- Assignment endpoint does not verify the assignee holds the role in scope or is active, and does not write an audit row (future work).
- Deactivate and transfer have no last-admin guard, self-deactivation guard, or second approval for admins (future work).
- Audit chain can fork under truly concurrent writers (single-writer SQLite assumed; production would serialise appends or use a database sequence).
- Transition denials return rule names to callers without audit:read (to be unified with the generic tier).
- PATCH /grants can currently lengthen valid_to; only shortening is intended (future work), valid_from backdating is not blocked.
- Parser has no service-key authentication; it is identified by its X-Actor-Id account. No POST /jobs create/update endpoint exists yet for the parser.
- Retention of audit history assumed 7 years, not automated.

