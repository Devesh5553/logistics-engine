# Nagarkot Forwarders Pvt. Ltd. (NFPL)
# Access Control Service: Architecture & Design Document

**Document Version:** 1.0.0  
**Target Audience:** Technical Leadership, Security Architects, and Review Committee  
**System Classification:** Mission-Critical Customs Brokerage Access Engine  

---

## 1. Executive Summary & Problem Context

Nagarkot Forwarders Pvt. Ltd. (NFPL) operates across 5 branches (Delhi, Mumbai, Kolkata, Chennai, Bengaluru) processing high-volume customs declarations for domestic and multinational enterprise clients. Each declaration is represented as a **Job** bound by four scope dimensions:
1. **Client** (`client_id`, e.g., `CL-001` to `CL-010`)
2. **Branch** (`branch_id`, e.g., `DEL`, `MUM`, `KOL`, `MAA`, `BLR`)
3. **Direction** (`direction`: `IMPORT` or `EXPORT`)
4. **Mode** (`mode`: `AIR`, `OCEAN`, `ROAD`, `RAIL`)

A job transitions through strict customs compliance stages:
$$\text{Confirmed} \longrightarrow \text{Prepared} \longrightarrow \text{Checked} \longrightarrow \text{Filed}$$
with exceptional branches to $\text{Cancelled}$ and $\text{Reopened}$.

### The Core Problem
Prior to this system, unauthorized mutation occurred on a `Filed` declaration submitted to Indian Customs (ICEGATE) without traceability. Management mandated an access control service that is **strict where it matters** (customs compliance, separation of duties, cryptographic auditability) yet **operationally frictionless** for daily clearing staff.

---

## 2. Core Architectural Model & Key Decisions

```
               ┌────────────────────────────────────────────────────────┐
               │                 FastAPI REST Service                   │
               │        (Header Authentication: X-Actor-Id)             │
               └──────────┬─────────────────────────────────┬───────────┘
                          │                                 │
           Action / Evaluation Requests              Work Queue Queries
                          ▼                                 ▼
               ┌──────────────────────┐          ┌──────────────────────┐
               │   decide() Engine    │          │  Indexed Queue SQL   │
               │   Single Decision    │◄────────►│ Compile decide() to  │
               │   Point (In-Memory)  │ (Parity) │ Single Indexed Query │
               └──────────┬───────────┘          └──────────┬───────────┘
                          │                                 │
       Evaluation Pipeline: Live Grants (No Caching)         │
       1. User Active & Not Absent Check                     │
       2. Scope Containment Check                            │
       3. Dynamic Role-Permission Resolution                 │
       4. Workflow State Machine Rule Match                  │
       5. Separation of Duties (Maker != Checker)            │
       6. Dual-Control High-Risk Token Validation            │
                          ▼                                 ▼
               ┌────────────────────────────────────────────────────────┐
               │                 SQLAlchemy ORM Layer                   │
               │          (SQLite Default / Postgres DDL)               │
               └──────────┬─────────────────────────────────┬───────────┘
                          │                                 │
                          ▼                                 ▼
               ┌──────────────────────┐          ┌──────────────────────┐
               │ Transactional DB     │          │ Append-Only Audit    │
               │ Optimistic Locking   │          │ SHA-256 Hash Chained │
               │ Version Increments   │          │ Decision Snapshots   │
               └──────────────────────┘          └──────────────────────┘
```

### 2.1 Access Model: Scoped Role-Based Access Control (RBAC with Attribute Constraints)
Rather than an unstructured ABAC or pure rigid RBAC, NFPL uses **Scoped RBAC**.
- **Roles & Permissions as Data:** Roles (`Maker`, `Checker`, `Manager`, `Admin`, `Auditor`, `Parser`, `Viewer`) and granular actions (`job:prepare`, `job:check`, `job:file`, etc.) reside in database tables (`roles`, `permissions`, `role_permissions`).
- **Scoped Grants:** A user receives a `Grant` linking them to a `Role` with optional containment attributes: `(client_id, branch_id, direction, mode)`. `NULL` acts as a wildcard ($\ast$).
- **No Caching:** Decisions always evaluate live grants. Revocations and end-dates take effect instantly ($0\text{ ms}$ propagation delay).

### 2.2 Storage & Engine Trade-offs
- **SQLite by Default, PostgreSQL DDL:** SQLite allows zero-dependency reviewer execution from the README while maintaining ACID transactional guarantees. Composite indexes (`ix_jobs_scope_stage`, `ix_grants_user_validity`) ensure sub-10ms queries across 200,000 scale records.
- **Optimistic Concurrency Control:** Every job carries an integer `version`. Mutations require `expected_version`, preventing lost updates or race conditions when multiple clearing agents act concurrently.
- **Single Decision Function (`decide()`):** All authorization flows through `decide(user, action, job, at)`. The work queue (`GET /users/{id}/queue`) compiles the exact same rules into SQL, verified by full parity tests across the entire dataset.

---

## 3. Answers to Brief Questions (Q1 to Q14)

### Q1: Should everyone see everything? If not, who sees what? How does that differ from who may act?
**Decision:** View and Act are completely decoupled into distinct permissions and explanation tiers.
1. **View Scoping:**
   - **Internal Staff:** Can view jobs within their branch/client scope.
   - **Client Users:** Tied strictly to their own `client_id`. Attempting to access other clients' jobs returns **HTTP 404** (identical to non-existent jobs) to prevent existence leakage.
   - **Internal Fields Privacy:** When client users view history (`GET /jobs/{id}/history`), internal staff names, internal remarks, and system snapshots are stripped; they see stage progressions only.
2. **See vs. Act Decoupling:**
   - Holding `job:view` grants read rights.
   - Stage progression requires specific action permissions (`job:prepare`, `job:check`, `job:file`), current workflow stage matching, and separation-of-duties clearance.

### Q2: How would you describe someone who is a Maker for every client but a Checker for only a few? What about someone who is a Checker for one client at one branch, but only for imports?
**Decision:** Expressed naturally through multi-grant scoping without role duplication:
- **Case A (Wide Maker, Narrow Checker):** User `usr_maker_wide` holds:
  1. `Grant(role='Maker', client_id=NULL, branch_id=NULL, direction=NULL, mode=NULL)`
  2. `Grant(role='Checker', client_id='CL-001', branch_id='DEL', direction=NULL, mode=NULL)`
  3. `Grant(role='Checker', client_id='CL-002', branch_id='DEL', direction=NULL, mode=NULL)`
- **Case B (Slice Checker):** User `usr_checker_del` holds:
  `Grant(role='Checker', client_id='CL-001', branch_id='DEL', direction='IMPORT', mode=NULL)`
  The evaluator enforces conjunction across all specified attributes. If the job is `EXPORT` or branch is `MUM`, access fails containment.

### Q3: What if the person who prepared a checklist is also qualified to approve it?
**Decision:** Mandatory Separation of Duties (Four-Eyes Principle / Maker-Checker Conflict).
- Even if a user holds valid `Checker` grants covering the job, `decide()` checks `job.prepared_by == user.id`.
- If matched, `job:check` is immediately denied with machine-readable code `MAKER_CHECKER_CONFLICT`.
- Furthermore, conflicting jobs are **automatically excluded from the user's queue**, ensuring they never see their own prepared jobs in their pending check list.

### Q4: What if the person responsible for a job is absent?
**Decision:** Time-boxed, non-transitive delegation with absence routing.
- Users mark absence via `is_absent=True`, `absent_from`, `absent_to`, and `absence_reason`.
- **Delegation Grant:** The delegator issues a child grant (`source='delegation'`, `parent_grant_id=parent.id`).
- **Guardrails:**
  1. Delegated scope cannot exceed delegator's own scope.
  2. Delegated `valid_to` cannot exceed delegator's own grant expiry or absence window.
  3. Re-delegation is strictly prohibited (`parent.source != 'delegation'`).
  4. While absent, the original owner's assigned jobs surface warning `needs_reassignment=True` and route to the active delegate's queue.

### Q5: What happens to jobs a person is halfway through when their responsibilities change?
**Decision:** Grants are end-dated; in-flight assignments are flagged, never silently stranded.
- When an employee changes roles or transfers, their grant is end-dated or revoked (`valid_to = now` or `revoked_at = now`). Grants are never overwritten or deleted.
- An automatic trigger flags active assignments for that user on uncompleted jobs (`needs_reassignment=True`, `reassignment_reason='Grant ended/revoked'`).
- The branch manager's queue displays these flagged jobs for explicit reassignment.

### Q6: Who should be able to cancel a filed job or reopen a closed one? What should it take?
**Decision:** High-risk operations require dedicated permissions, mandatory written reasons, and pre-authorized dual control.
- **Stages:** `Filed` is locked with Indian Customs ICEGATE. `Cancelled` is terminal.
- **Requirements:**
  1. Actor holds `job:cancel_filed` or `job:reopen_closed`.
  2. Mandatory written non-empty `reason`.
  3. **Dual Control Token:** An `Approval` record created via `POST /approvals` approved by an independent second officer (`approved_by != requester`).
  4. Tokens are **single-use** (`used_at` set upon execution) and **time-limited** (auto-expire in 24h). Attempted reuse returns `403 APPROVAL_ALREADY_USED`.

### Q7: What if a client’s own employee needs to follow their shipments?
**Decision:** Client-side read-only tenant isolation.
- User record has `user_type='client_user'`, `client_id='CL-001'`.
- Scoped strictly to `CL-001`. They cannot execute workflow actions (`job:prepare`, `job:check`, `job:file` are denied).
- When fetching job details or history, internal officer names, notes, and ICEGATE internal flags are omitted.
- Accessing any other client's job returns generic `404 Not Found`.

### Q8: What if an admin needs to fix a stuck job? What if an admin tries to make themselves more powerful?
**Decision:** Dual-control administrative guardrails.
- **Stuck Jobs (Admin Override):** Admin can invoke `POST /jobs/{id}/override` with mandatory `reason`. If the job is `Filed` or `Locked`, a second admin's approval token is required. Every override logs an un-bypassable `OVERRIDE` audit record.
- **Self-Escalation Prohibition:**
  1. Admins cannot create grants for themselves (`actor_id != target_user_id`).
  2. Modifying an admin's own scope requires dual-admin approval.
  3. Administrative permissions (`grant:manage`, `admin:override`, `user:manage`) cannot be delegated.

### Q9: How should an automated parser that creates and updates jobs overnight be treated?
**Decision:** Service account with stage-locked principle of least privilege.
- Account `usr_parser` (`user_type='service_account'`).
- Granted narrow role `Parser`: permissions restricted strictly to early stages (`Confirmed`, `Prepared`).
- Evaluator enforces stage limits: the parser is hard-blocked from filing (`job:file`), cancelling, reopening, or mutating locked/filed jobs.
- All EDI mutations explicitly log `actor_id='usr_parser'` in `job_events` and `audit_log`.

### Q10: How do you answer “why can she do this and he can’t?”
**Decision:** Deterministic Differential Comparison API (`GET /compare`).
- The endpoint evaluates User A and User B side-by-side against the same action and job.
- It returns structured explanations:
  1. User status (active, absent, leaver).
  2. Scope match/mismatch on Client, Branch, Direction, Mode.
  3. Role permission presence.
  4. Separation-of-duties check (did either user prepare the job?).
  5. High-risk dual control requirements.
- Explanation tiers: Auditors and Admins receive full internal evaluation traces (`tier='full'`); standard callers receive sanitized explanations (`tier='generic'`).

### Q11: How do you answer “who could have changed this job on the 3rd?”
**Decision:** Temporal Grant Replay API (`GET /jobs/{id}/who-could?on=DATE`).
- Evaluates the database state as of `on=DATE`.
- Identifies all grants where `valid_from <= DATE` and `(valid_to IS NULL OR valid_to >= DATE)` and `(revoked_at IS NULL OR revoked_at >= DATE)`.
- **Key Capability:** Even if an employee was terminated on the 5th and their grant revoked, querying for the 3rd accurately lists them and the exact grant ID that empowered them on that day.

### Q12: How would a new kind of role be introduced next quarter while engineering is busy?
**Decision:** Roles and permissions are 100% data-driven.
- A business administrator creates a new role (e.g., `ExportSpecialist`) and assigns permissions via configuration/SQL inserts into `roles` and `role_permissions`.
- Zero code changes, zero deployments, and zero schema migrations required.

### Q13: How do you make sure a person’s “jobs waiting for me” count matches what they can actually do?
**Decision:** Code compilation of `decide()` into indexed SQL.
- The work queue query compiles the exact predicates from `decide()`:
  - Active grants in scope
  - Stage mapping matching the role's act permission
  - Exclusion of jobs where `job.prepared_by == user.id` (maker-checker separation)
- Single SQL query executes `COUNT(*)` and fetches the paginated job list in the same transaction, preventing count drift. Verified by full-sample parity tests.

### Q14: What should happen when your system is unsure?
**Decision:** Fail-Closed Default (Deny, Log, Explain).
- If any required parameter is missing, if a grant is ambiguous, or if an unexpected exception occurs, the system defaults to:
  $$\text{Decision} = \text{DENY}$$
- The failure is recorded in the append-only audit log with `deny_code='FAIL_CLOSED_TRIGGERED'` and returns a safe HTTP error to the caller.

---

## 4. Answers to Added Questions (Q15 to Q19)

### Q15: Job Scope Edits (Branch, Client, Direction, Mode)
- Editing shipment scope is a high-risk mutation (`job:edit_scope`).
- **Guardrails:**
  1. A `Filed` job's scope is strictly locked; it cannot be edited without reopening.
  2. The actor must hold rights over **both** the old scope and the new scope (e.g., a Delhi manager cannot transfer a job to Mumbai unless they hold Mumbai managerial rights).
  3. Pre-authorized dual control approval is required.

### Q16: Leavers and Branch Transfers
- **Leavers:** Atomic deactivation ends all active grants and delegations on timestamp `T`, cancelling active assignments and setting `needs_reassignment=True`.
- **Transfers:** Implemented as an atomic transaction that end-dates old branch grants and creates new branch grants on the same timestamp, ensuring no gap in coverage or orphaned privileges.

### Q17: Concurrency & Optimistic Locking
- All state transitions and scope edits check `expected_version == job.version`.
- Updates increment `version = version + 1`.
- If two users submit simultaneous updates, one commits and the second receives `409 Conflict`, requiring them to refresh state before proceeding.

### Q18: Audit Trust, Hash Chaining & Denial Tracking
- Every audit entry stores SHA-256:
  $$\text{record\_hash}_i = \text{SHA256}(\text{prev\_hash}_{i-1} \,\|\, \text{timestamp} \,\|\, \text{actor} \,\|\, \text{action} \,\|\, \text{decision} \,\|\, \dots)$$
- `GET /audit/verify` traverses the chain from genesis block (`000...000`) and recomputes hashes. Any out-of-band row tampering or deletion breaks the chain and is pinpointed immediately.
- `GET /audit/reports/filed-job-denials` monitors repeated unauthorized attempts against filed declarations (early warning incident detection).

### Q19: Count and Explanation Leaks (Tenant Isolation)
- Client users querying out-of-scope jobs receive identical `404 Not Found` responses to non-existent jobs.
- Queue counts do not reflect jobs outside their authorized slice. Timing differences are mitigated through indexed index-seek lookups.

---

## 5. Assumptions and Deliberate Cuts

| Area | Decision / Cut | Rationale |
| :--- | :--- | :--- |
| **Audit Retention** | Assumed 7 years (customs regulatory standard) | Documented operational policy rather than automated lifecycle purge script. |
| **Hash Anchor** | Stored within SQLite `audit_log` table | Enterprise deployment would anchor the head hash externally (e.g., AWS QLDB or RFC 3161 timestamping authority). |
| **Frontend Dashboard** | Deliberately excluded; API-first design with Swagger UI & CLI walkthrough | Core customs access control engine is headless; operators interact via API/CLI; Swagger `/docs` provided for interactive exploration. |
| **Database** | SQLite for local review; PostgreSQL DDL provided | Eliminates reviewer Docker/Postgres setup friction while preserving production-grade relational DDL. |
