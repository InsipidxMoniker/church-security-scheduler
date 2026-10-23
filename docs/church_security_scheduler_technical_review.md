# Church Security Volunteer Scheduler: technical review

Date: October 23, 2026  
Reviewed source: attached specification v1.0, sections 1–14 and AC-01–16/T01–40.  
Companion specification: `church_security_scheduler_mvp_spec.md`, v1.1.

## Verdict and severity

The MVP is implementable with the proposed single-host stack. The original is not yet a sufficiently precise production implementation contract. The principal risks are stale approvals, publication races, ambiguous solver optimality, and historical outputs that change when live data changes. These are specification defects, not evidence of vulnerabilities in existing code. No implementation or penetration testing was performed.

Critical means a missing invariant can permit an unauthorized publication or invalidate the central scheduling guarantees. High means incorrect schedules, historical corruption, or material security exposure is plausible. Medium means predictable operational or implementation ambiguity. Low means optional polish or avoidable complexity.

The revised specification resolves technical choices without changing staffing rules. Actual position classifications remain a required setup decision. No further business decision blocks development. Production release remains blocked until the revised integrity/security tests, setup confirmation, and measured scale/restore checks pass.

## Critical findings

| ID | Source | Finding and concrete correction |
|---|---|---|
| C01 | §§4.3, 7–8; AC-14 | Draft revision alone cannot detect a volunteer availability update, roster deactivation, qualification change, or configuration change during generation/publication. Add separate date-input and global-input epochs, compare all epochs on commit, and revalidate live eligibility inside a short write transaction at publication. Serialize publication with availability submission. An update that commits first must be included or cause a conflict; a submission after publication must be rejected. |
| C02 | §§4.2–4.3, 7; T14/T20 | Binding an approval to assignment IDs is insufficient because those rows can be edited in place, and an A→B→A edit can resurrect an old approval. Add monotonically increasing slot revisions, exact content fingerprints, explicit invalidation timestamps, same-version composite references, and final transactional checks. Never reactivate an invalidated approval. Manual same-zone proposal and approval must commit atomically. |
| C03 | §§4.4, 10; T33 | Sequential optimization is underspecified when a higher stage returns FEASIBLE. Freezing an unproven coverage value and optimizing preferences can imply a false lexicographic guarantee. Continue to the next priority only after the current optimum is proved. Otherwise stop and return the legal incumbent, visibly nonoptimal. Maintain one total budget and retain the last valid incumbent if a later stage returns UNKNOWN. |
| C04 | §§7, 8; AC-12/13 | Independent foreign keys allow a slot to refer to a service from another date, and the schema permits multiple drafts/current publications. Publication atomicity and historical immutability are incomplete. Use date-scoped composite FKs, one draft per date, one current-publication pointer, unique per-date publication numbers, a single publication transaction, and database protection for published children. Historical payloads must include volunteer names, timezone, service labels/times, classification, exceptions, and approvals. |

## High findings

| ID | Source | Finding and concrete correction |
|---|---|---|
| H01 | §§4.1–4.2, 6; T21/T22 | An approved locked same-zone pair conflicts with the rule that automatic scheduling always enforces rotation. Preserve strict automatic rotation: reject regeneration while such a pair is locked, explain why, and let the scheduler finish manually or explicitly unlock/change it. Never silently extend an exception to another pair or let the solver invent exceptions. |
| H02 | §§5, 8–9 | PIN hashing alone leaves a small credential space and public verification endpoints exposed to guessing and resource exhaustion. Define generated PIN length, hash work factors, IP/person/global throttles before hashing, bounded hashing concurrency, session invalidation on reset, and token redaction across proxy and app. Warn that six-digit PINs have limited offline resistance; use eight-digit random PINs by default, preserving the six-digit minimum option. |
| H03 | §§2, 5, 8 | Session scope and object authorization are not concrete. Enforce volunteer+date+link-generation scope server-side; derive the volunteer from the session, not request IDs. Restrict every admin, history, export, summary, and approval endpoint by role. Public links expose names only, never schedules or other responses. Test ID substitution and cross-date access. |
| H04 | §§2, 9–10 | MFA is both preferred and required, and bootstrap/recovery/session behavior is missing. Require TOTP for both staff roles, define one-time bootstrap and local recovery, hash one-time recovery codes, encrypt TOTP seeds using a key outside SQLite, revoke sessions on account changes, and prevent removal of the last active administrator. No email subsystem is necessary. |
| H05 | §§6–8 | Published versions claim immutability but lack display-name snapshots and treat superseding as a mutation. Store a canonical immutable published payload and derive superseded status from the current-publication pointer. Amendments copy into new drafts; approvals must be reviewed and reissued for each new version. Live deactivation must flag drafts, not alter historical exports. |
| H06 | §§4.1, 6; T23 | “Mark/remove” stale assignments allows silent destructive edits and inconsistent locks. Keep conflicting draft rows and locks visible, flag them, block publication, and require explicit repair. Regeneration can replace invalid unlocked rows; invalid locked rows block it. |
| H07 | §§7–8; T30/T31 | Two-step swaps can fail uniqueness constraints or leave half a swap; retry semantics can duplicate actions. Validate and apply a multi-slot edit in one transaction, clear touched rows internally before refilling, and roll back on any error. Scope publish idempotency keys to actor/date/operation and bind them to a request digest. |

## Medium findings

| ID | Source | Finding and concrete correction |
|---|---|---|
| M01 | §§1, 9 | The 20-person timing target and fixed-looking “at most four qualified” wording can become artificial limits. Treat those counts as initial conditions only. Benchmark 100 volunteers across every Sunday in a five-Sunday month, with eight positions and two services each, using independent Sunday operations. No monthly scheduling UI, fairness, or cross-date optimization is added. |
| M02 | §§4.4, 11 | A fixed seed alone is not a deterministic tie-break, and sum-of-ranks ties can remain. Use canonical input ordering, one solver worker, and a final slot-by-slot lexicographic feasibility tie-break after proving all objectives. Give canonicalization the same total deadline; report incomplete canonicalization instead of promising repeatability under wall-time interruption. |
| M03 | §§3, 7; T29 | It is unclear when changed classifications enter an existing draft. Give drafts explicit configuration snapshots. New drafts capture current settings; existing drafts adopt changes through a reviewed refresh. Publication requires current setup/config compatibility, otherwise a conflict and refresh. Published snapshots never refresh. |
| M04 | §§5–6 | Missing/partial availability, duplicate names, token expiry/rotation, and amendments lack exact behavior. Store both service answers atomically as one two-boolean response; absent row is no response. Use immutable IDs and administrator-chosen distinguishing display names. Expiry does not reopen publication; replace links explicitly, revoke old link sessions, and never reopen self-service after first publication. |
| M05 | §§7, 9; T38/T39 | Backups do not specify SQLite WAL consistency, encryption-key recovery, or failure visibility. Use SQLite's backup API, include required external keys/config in a protected recovery bundle, check integrity and foreign keys after restore, and invalidate restored web sessions. Daily backup and 30-copy retention are defaults, not an asserted disaster-recovery guarantee. |
| M06 | §§4.4, 9 | A solver invoked directly in an async request can monopolize a small server. Use one local bounded solver subprocess, no DB transaction held during solving, an app-wide generation semaphore, and bounded request hashing/PDF work. Busy requests get a retry response. Redis/Celery and distributed infrastructure are unnecessary. |
| M07 | §§10–11 | Existing tests cover happy paths but not objective proof, approval resurrection, cross-date references, public IDOR, CSRF, reset-session revocation, and actual database races. Retain T01–40, add an independent small-instance exhaustive oracle and real-SQLite multi-connection race tests, plus defined scale gates. Do not assert optimality for timeout incumbents. |
| M08 | §§2, 6, 8 | Scheduler roster capabilities and Administrator-only PIN/settings capabilities overlap. Specify an operation matrix: both manage roster eligibility and scheduling; only Administrator manages credentials/PINs, staff, and global settings. A Scheduler-created volunteer remains unusable until an Administrator provisions a PIN. Define a minimal CSV import because import is already in the original scope. |

## Low findings and optional improvements

| ID | Source | Recommendation |
|---|---|---|
| L01 | §7 | A general qualifications catalog and separate service-instance table add joins without MVP value. A boolean restricted qualification and service number 1/2 preserve the same business rules. Use the simplified schema in v1.1. |
| L02 | §9 | PostgreSQL, Kubernetes, external queues, full SPA, SSO, telemetry platforms, and automated Signal integration are unnecessary at this scale. Retain Compose, SQLite, server-rendered pages, and a local solver process. Reconsider infrastructure only from measured constraints. |
| L03 | §§6, 11 | An isolated responsive browser smoke test and explicit text labels are sufficient for core usability; do not rely on color alone. Automated accessibility checks supplement, rather than replace, a phone-width manual workflow check. |

## Scheduling analysis and preserved decisions

The position/zone mapping is internally consistent: four outside and four inside positions per service. Filling both restricted positions at both services without exceptions requires four distinct qualified people, but qualification count alone is not sufficient. Availability, other locks, and zone capacity must also permit placement. T03 correctly needs a compatible fixture, not just a count assertion.

The priority order is essential coverage, optional coverage, people serving twice, stability, then deterministic tie-breaking. “Prefer both” remains a preference and cannot reduce coverage. With equal total assignments, maximizing double-service people also minimizes distinct assigned people. Across different coverage totals, those formulations are not interchangeable, so coverage must be fixed first.

A legal all-vacant solution exists if there are no conflicting locks. INFEASIBLE on an unlocked, vacancy-permitting model indicates an implementation defect. Explanations should state shortages or constraints conservatively; proving that a particular slot is intrinsically unfillable would require an additional solve and is unnecessary for MVP.

Essential/optional classification remains a setup input. No hierarchy among essential positions is invented. A stable technical tie-break decides equivalent coverage choices. Approvals permit only same-zone pairs and specific essential vacancies; availability, qualification, inactivity, and per-service exclusivity are never overridable.

## Scope and release gates

There are no unresolved business-rule blockers after adopting v1.1. Deployment still needs the actual essential list, church timezone, service labels, roster, credential distribution, DNS/TLS, and backup destination. These are configuration inputs, not reasons to defer development.

Release gates: preserve all original AC/T behaviors with the explicit clarifications in v1.1; pass added security/concurrency/oracle tests; benchmark 100 volunteers and a five-Sunday dataset; restore a backup including authentication keys; record remaining limitations. Claims of production readiness require these implementation results, not merely this review.

## Primary technical references

These sources support the technology checks. The product policies and implementation defaults in v1.1 are explicit design recommendations, not requirements attributed to the sources.

- CP-SAT statuses and integer constraints: https://developers.google.com/optimization/cp/cp_solver
- SQLite isolation and write serialization: https://sqlite.org/isolation.html
- SQLite composite foreign keys and per-connection enforcement: https://sqlite.org/foreignkeys.html
- SQLite consistent online backups: https://sqlite.org/backup.html
- OWASP password hashing: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- OWASP authentication and throttling: https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
- OWASP sessions: https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html
- FastAPI concurrency guidance: https://fastapi.tiangolo.com/async/
