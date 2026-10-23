# Codex implementation handoff: Church Security Scheduler MVP

Use `church_security_scheduler_mvp_spec_v1.2.md` as the **single authoritative contract**. The accompanying `church_security_scheduler_technical_review.md` is supporting context, not a competing specification. Versions 1.0 and 1.1 are superseded. Do not reinterpret or silently replace confirmed business rules.

## Objective

Implement a production-ready, self-hosted, mobile-friendly Sunday church security volunteer scheduler. Preserve exactly two services and eight positions per service, essential/optional staffing, service-specific availability verified by personal PIN, the required zone rotation and narrow approved exceptions, immutable publications, Signal-ready text and PDF, individual staff accounts with MFA, and audit history. No automatic Signal integration or cross-week fairness optimization is in scope.

## Working method

1. Read the entire v1.2 specification, especially §§2–6 (authorization, revisions, scheduling), §§8–10 (integrity, races, security), and §§13–16 (tests, performance, release).
2. Begin by proposing a concise architecture and implementation plan mapped to the specification's phases, plus a requirement-to-test traceability matrix. Distinguish actual spec ambiguities from coding choices. Do not change or expand product scope.
3. Implement **Phase 1 only** first: repository and pinned dependencies, Docker Compose and reverse-proxy foundation, migrations/SQLite constraints, staff bootstrap, authentication/TOTP/session controls, role authorization, audit events, initial settings/position schema, test fixtures, and foundation-level security/integrity tests. Include minimal operator instructions. Do not claim the full scheduler is complete at this stage.
4. Run applicable tests against real migrated SQLite files and report test commands/results, any failures, and limitations. Show which requirements are met and which are deferred to later phases. Stop at the Phase 1 checkpoint for review before beginning Phase 2.
5. On approval, complete later phases in the v1.2 implementation order with tests at each checkpoint: volunteer/PIN/availability flow; CP-SAT scheduler plus independent exhaustive oracle; draft editing/approvals/publication/amendment/exports; end-to-end security, concurrency, backup/restore, and 100-volunteer/five-Sunday performance validation.

## Implementation guardrails

- v1.2 §6 tie-breaking uses a reproducible SHA-256 Sunday-specific volunteer rank at fixed proven E/O/B/U. Do not substitute fixed UUID sorting, runtime randomness, or fairness history.
- An approved essential vacancy may remain vacant even when an eligible volunteer exists. The same Scheduler may approve and publish, with independent audit records.
- Already-invalid drafts must support incremental conflict-reducing repair without accepting new/worsened hard conflicts. Publication must still reject all outstanding hard-rule violations.
- A global timezone-only change does not change existing dates' timezone snapshots or stale their drafts. `settings_revision` and schedule-applicable `config_epoch` have distinct purposes.
- Keep generation outside DB transactions, validate against input epochs on commit, and retain historical publication payloads unchanged.
- Use server-side auth and verification, secure token/PIN handling, rate limits, CSRF protection, role checks, and no public schedule viewer. Never use real volunteer data in fixtures.
- Preserve the v1.2 hard constraints, atomicity, and versioning. Do not ship a happy-path-only generator or mark unproven CP-SAT results as optimal.

## Required phase reporting

For each checkpoint provide: changed files, architecture decisions, migration/test commands, test results with passing/failing/skipped counts, acceptance criteria now satisfied, requirements not yet implemented, security/operational caveats, and instructions to review the result locally. A feature is **not** production-ready until the complete v1.2 acceptance and release checks pass. If there is a genuine requirement contradiction, quote the conflicting passages and request one decision instead of silently inventing a policy.
