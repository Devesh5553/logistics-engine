# NFPL Access Control Service — Design Document

Author: Devesh · Reference date used throughout: 2026-01-15

---

## 1. Problem and scope

NFPL operates 5 branches (Delhi, Mumbai, Kolkata, Chennai, Bengaluru) processing customs
declarations for about 10 clients. Each declaration is a **Job**, scoped by four dimensions:

1. **Client** (`client_id`, e.g. `CL-001`–`CL-010`)
2. **Branch** (`branch_id`: `DEL`, `MUM`, `KOL`, `MAA`, `BLR`)
3. **Direction** (`direction`: `IMPORT` or `EXPORT`)
4. **Mode** (`mode`: `AIR`, `FCL`, `LCL`)

A job moves through: **Confirmed → Prepared → Checked → Filed**, with side branches to
**Cancelled** and **Reopened**.

**The problem:** a filed shipment was altered by someone with no business touching it, and
nobody could say who had been allowed to, or why. This service decides who may do what on
which job, explains every answer, and keeps a record that cannot be quietly edited.

---

## 2. Architecture

```
                 FastAPI REST service (caller identified by X-Actor-Id)
                               |
            ----------------------------------------
            |                                      |
     decide(user, action,                   Work queue
       job, at)  <--parity-->        (same rules, compiled to one
    single decision point              indexed SQL query)
            |                                      |
    Evaluation order, stops at first failure:
    1. user active, not absent (for acting)
    2. grant scope contains the job
    3. role carries the permission
    4. job stage allows the action
    5. separation of duties (preparer != checker)
    6. high-risk actions: reason + second approval
            |                                      |
            ----------------------------------------
                               |
                SQLAlchemy ORM (SQLite by default;
                 schema written to be Postgres-compatible,
                 not independently verified against Postgres)
                               |
            ----------------------------------------
            |                                      |
      Jobs: version column,                Audit log: append-only,
      checked on every write               SHA-256 hash-chained,
      (optimistic locking)                 stores the decision trace
```

### 2.1 Access model: scoped roles, not pure RBAC or ABAC

Roles (`Maker`, `Checker`, `Filer`, `Manager`, `Admin`, `Auditor`, `Viewer`, `ClientUser`,
`Parser`) and their permissions (`job:prepare`, `job:check`, `job:file`, …) live in tables, not
code. A **grant** ties a user to a role plus an optional scope — client, branch, direction,
mode — where a blank field means "all". This gives attribute-level precision without the
complexity of full ABAC, and a non-technical reader can read one grant and understand it.

**Trade-off:** scoped RBAC cannot express a rule that depends on something outside these four
fields (e.g. "only during business hours"). We accepted this because the brief's scenarios
never required it, and a richer policy language would cost more to explain and audit than it
would add here.

### 2.2 No caching

Every decision reads grants live. A revoked grant stops working on the next request; there is
no cache to invalidate and no propagation delay to reason about.

**Trade-off:** this costs a database query per decision. At the brief's scale (500 users,
200,000 jobs) this stays well under typical request latency with the indexes described below;
it would need revisiting at a much larger scale.

### 2.3 Optimistic locking

Every job carries an integer `version`. A write must supply the version it last read; a stale
version is rejected rather than silently overwritten.

**Known limit:** the check-then-increment must be done as a single conditional update (`UPDATE
... WHERE version = ?`) to be safe under real concurrency; a plain read-then-write in
application code is not atomic on its own. See the concurrency test for how this is verified.

---

## 3. Answers to the brief's questions (Q1–Q14)

### Q1 — Should everyone see everything?

No. Viewing and acting are separate permissions.

- **Internal staff** see jobs within their grant's scope.
- **Client users** see only their own `client_id`. A job outside their scope returns the same
  404 as a job that does not exist, so nobody can probe for other clients' shipments.
- **Internal fields** (who prepared it, internal notes) are hidden from client users even on
  jobs they can see.

Acting requires everything viewing does, plus the job's current stage matching the action, and
clearing the separation-of-duties check below.

**Trade-off:** denying with a 404 instead of a 403 for out-of-scope jobs means a legitimate user
who mistypes a job ID gets an unhelpful "not found" rather than "wrong job." We chose privacy
over that convenience.

**Test:** `test_client_user_isolation_and_obfuscation`

### Q2 — Wide maker / narrow checker; one-client-one-branch-imports checker

Expressed as multiple grants, no role duplication needed.

- **Wide maker, narrow checker:** one grant `Maker` with every scope field blank, plus one
  `Checker` grant per client they may check (e.g. `Checker` for `CL-001`, all branches; a
  second `Checker` grant for `CL-002`, all branches).
- **Fully scoped checker:** one grant: `Checker`, `client=CL-001`, `branch=DEL`,
  `direction=IMPORT`, `mode=blank`. All four fields are checked together — an export job, or a
  job in any other branch, fails the scope check.

**Trade-off:** a person with many narrow grants is harder to read at a glance than one row with
a condition language. We accept this because each grant stays simple enough to state in one
plain sentence, which matters more for an auditability-first system.

**Test:** `test_wildcard_maker_with_narrow_checker_grants`, `test_narrow_checker_slice_enforcement`

### Q3 — Preparer is also a qualified checker

Separation of duties is enforced unconditionally. If `job.prepared_by == user.id`, `job:check`
is denied with `MAKER_CHECKER_CONFLICT`, even if the user holds a fully qualified Checker grant
for that job. There is no override; a different, qualified checker must act. Jobs a user
prepared are also excluded from their own queue, so they never appear as "waiting for me."

**Trade-off:** this can stall a job if the only checker in scope is also its preparer. We judged
this an acceptable cost, since the alternative (an override) reopens exactly the hole the brief
describes.

**Test:** `test_maker_checker_conflict_separation_of_duties`

### Q4 — The responsible person is absent

A user can be marked absent, with a start and end date. While absent, they may still **view**
jobs but may not **act** on them (`USER_ABSENT`). Someone else can be given a **delegation**: a
grant whose source is `delegation`, linked to the absent person's own grant, with its own
expiry.

**Guardrails:**
1. A delegated grant's scope cannot exceed the delegator's own scope.
2. Its expiry cannot exceed the delegator's own grant expiry.
3. A delegated grant cannot itself be re-delegated.
4. The delegation ends automatically when its `valid_to` passes, and should end immediately if
   the delegator's own grant is revoked (see Q5).

**Trade-off:** absence has to be set explicitly by an admin; the system does not infer absence
from inactivity. We accepted this because inferring absence incorrectly (e.g. during a quiet
week) is worse than requiring one manual step.

**Test:** `test_inactive_users_and_grant_expiry` and the delegation-window tests

### Q5 — Responsibilities change mid-job

Grants are never edited in place, only end-dated or revoked, so history is never lost. When a
grant that covers an in-flight assignment ends, that assignment is flagged
(`needs_reassignment=True`, with a reason) rather than silently left in place. A flagged job
still needs a person with the right scope to pick it up — this depends on whoever reviews
flagged assignments noticing them; we have not built a dedicated "flagged work" queue view
beyond the flag itself (see Known Limits).

**Test:** the grant-revocation / in-flight-assignment tests

### Q6 — Cancelling a filed job, reopening a closed one

Both are high-risk actions, requiring the **Manager** role in the job's scope (not Admin —
administrators manage access, not shipments), a mandatory written reason, and a second,
independent approver who holds the same permission in scope. Approvals are single-use and
expire. `Cancelled` is not a dead end: a cancelled job can be reopened through the same
dual-control path, which is why planted test data includes a cancelled job used specifically to
exercise reopening.

**Trade-off:** requiring a second approver for every filed-job change adds friction to a
genuine emergency fix. We accept this because the brief's incident was exactly an emergency-style
unilateral change; the admin override path (Q8) exists for operational fixes that are not
scope or cancellation changes.

**Test:** the dual-control cancel/reopen tests

### Q7 — A client's own employee needs to follow their shipments

A `ClientUser` account is tied to exactly one `client_id` and holds only `job:view`. They never
hold any act permission. Another client's job, or a job that does not exist, both return an
identical 404.

**Test:** `test_client_user_isolation_and_obfuscation`

### Q8 — Admin fixes a stuck job; admin tries to self-escalate

An admin can override a stuck job through a dedicated endpoint, with a mandatory reason; if the
job is filed or locked, a second admin's approval is also required, so one administrator cannot
unilaterally rewrite a filed job. Separately:

1. An admin cannot grant a role to themselves.
2. Any change to an admin-level grant (one carrying `grant:manage`, `admin:override`, or
   `user:manage`) requires approval from a second, independent admin.
3. Admin-level permissions cannot be delegated.

**Trade-off:** dual control for admin changes slows down legitimate administrative work, not
just abuse. We accept this because the brief specifically names self-escalation as a case to
guard against.

**Test:** the admin self-escalation and override tests

### Q9 — The overnight parser

The parser is a service account holding a narrow `Parser` role limited to the early stages
(`Confirmed`, `Prepared`). It cannot file, cancel, reopen, or act on a locked job. Its actions
are attributed to its own account in the job history and audit log, the same as any other user.

**Known limit:** there is currently no `POST /jobs` endpoint for the parser to create new jobs
via the API; today it is identified and scoped correctly, but the actual overnight creation
path has not been built end-to-end. See Known Limits.

**Test:** `test_parser_stage_restrictions`

### Q10 — "Why can she do this and he can't?"

`GET /compare` runs the same decision pipeline for both people on the same action and job, and
reports the first point where their outcomes diverge — the first rule, in evaluation order,
where one passes and the other fails (scope, role, stage, separation of duties, or approval).
Auditors get the full trace, including grant IDs; everyone else gets a generic explanation with
no internal detail.

**Test:** `test_compare_access_and_differential_summary`

### Q11 — "Who could have changed this job on the 3rd?"

`GET /jobs/{id}/who-could?on=DATE` replays grants that were active on that date — not today's
grants — including ones since revoked. We treat the date as "any grant active at any point
during that calendar day."

**Known limit:** this currently replays grant history only. If the job's own scope, stage, or a
role's permissions changed after that date, those changes are not replayed, so the answer can
be wrong for a job whose scope was edited later. Closing this fully would mean versioning
`job_events` and `role_permissions` the same way grants are versioned; we cut this for time (see
Known Limits).

**Test:** the historical-grant-replay tests

### Q12 — Introducing a new role next quarter

Roles and permissions are rows in `roles`, `permissions`, and `role_permissions`. Adding a role
means adding rows through the same grant-management API used for everything else (not raw SQL
against the database, which would bypass the audit trail), so no code change or deployment is
needed, and the change is itself logged like any other administrative action.

**Test:** a new role created at runtime and exercised through `decide()`

### Q13 — "Jobs waiting for me" matches what I can actually do

The queue is not a separate set of rules. It compiles the exact predicates `decide()` uses
(active grant in scope, role permits the action, stage matches, preparer excluded) into one SQL
query, so the count and the list can never drift from what `decide()` would say about each job
individually. This is checked by a parity test across the full sample dataset.

**Test:** the queue/decide parity test

### Q14 — The system is unsure

Fail closed: any missing parameter, unrecognized value, ambiguous state, or unexpected exception
results in denial, not a guess, and the denial is logged with a distinct code
(`FAIL_CLOSED_ERROR` in the current implementation) so it can be told apart from an ordinary
rule-based denial.

**Test:** the fail-closed / forced-exception tests

---

## 4. Answers to added questions (Q15–Q19)

### Q15 — Editing a job's scope (client, branch, direction, mode)

Treated as high-risk: it needs `job:edit_scope`, a written reason, and the actor must hold
rights over **both** the current scope and the proposed new scope — a Delhi-only manager
cannot move a job to Mumbai. A filed job's scope cannot be edited directly; it must go through
the reopen flow first.

**Test:** the scope-edit boundary tests

### Q16 — Leavers and branch transfers

Deactivating a user is one atomic operation: it ends every grant and delegation they hold or
gave, and flags their in-flight assignments. A branch transfer is a paired operation — end the
old-branch grants and start the new-branch grants on the same timestamp — so there is never a
gap or an overlap in coverage.

**Known limit:** there is currently no "last remaining admin" guard and no self-deactivation
guard; see Known Limits.

**Test:** the leaver-deactivation and branch-transfer tests

### Q17 — Concurrency

Writes carry an `expected_version`; a mismatch is rejected with a conflict response rather than
silently overwritten.

**Known limit:** the comparison and the increment need to happen as one atomic database
operation (a conditional `UPDATE`), not a read followed by a separate write — otherwise two
near-simultaneous requests could both read the same version before either writes. This is
called out explicitly so it is tested, not assumed.

**Test:** the concurrent-write test

### Q18 — Can the audit trail be trusted?

Each audit row stores a hash of the row before it, so altering an old row breaks every hash
after it; `GET /audit/verify` recomputes the chain and reports exactly where it breaks. Denied
attempts are logged, not just successful actions, which is the direct fix for "nobody could say
who had been allowed to."

**Known limit:** this detects tampering with a row in the middle of the chain. Deleting rows
from the very end of the chain would not be detected without storing the latest hash somewhere
outside the database (e.g. an external timestamp authority) — we did not build that, and note
it as a limit rather than claiming full protection.

**Test:** `test_denied_attempt_audit_logging_hook` and the tamper-detection test

### Q19 — Do counts or explanations leak information?

Out-of-scope and non-existent jobs return identical 404s, so probing cannot confirm whether a
job exists. Explanations have two tiers: a full trace (grant IDs, rule names) for auditors, and
a generic message for everyone else, so a denial reason itself cannot be used to infer details
about grants or other people's access.

**Test:** `test_explanation_tiers`

---

## 5. Known limits and deliberate cuts

| Area | What was cut or limited | Why |
|---|---|---|
| Caller identity | Identified by an `X-Actor-Id` header, not a login | This is a demo authentication mechanism, not production auth; out of scope for this brief |
| Database | SQLite by default; schema written to be Postgres-compatible but not independently tested against a real Postgres instance | Removes setup friction for reviewers running this locally |
| Hash anchoring | The audit chain's head hash is stored only inside the same database | Production would anchor it externally (e.g. a timestamping authority) so even deleting trailing rows would be detectable |
| Parser | Scoped and permission-limited correctly, but no `POST /jobs` endpoint exists yet for its overnight creation job | Cut for time; the access-control shape is in place, the ingestion pipeline is not |
| Reassignment | Ended grants flag in-flight assignments, but there is no dedicated queue view for flagged work beyond the flag itself | Cut for time |
| Admin safety | No "last remaining admin" guard and no self-deactivation guard | Lower priority than the self-escalation guards already built |
| Who-could-have-changed | Replays grant history only, not job-scope or role-permission history | Full point-in-time replay of every dimension was cut for time |
| Concurrency | Optimistic locking depends on the write being a single conditional update, not a separate read-then-write | Flagged explicitly so it is verified by a dedicated test rather than assumed |
| Frontend | No dashboard was built as part of the submission; a separate evaluator-facing UI exists outside this design but is not part of the core engine | The brief asks for code, tests, a design document, data, and a demo — not a UI |
| Audit retention | Assumed 7 years (a customs record-keeping assumption), not automated | Documented policy rather than an automated purge job |
