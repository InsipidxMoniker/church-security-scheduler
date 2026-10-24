# Changelog

## v1.0.0 — Initial release (prepared; not yet published)

- Volunteer roster/qualifications, searchable public labels and atomic CSV preview/import.
- Sunday links, personal 4–12-digit PINs, service-specific availability and confirmed staff changes.
- Two-service/eight-position CP-SAT schedules with essential/optional coverage priorities, double-service preference, strict automatic zone rotation, locks and deterministic Sunday-specific tie-breaking.
- Manual edits/swaps/repair, exact-pair rotation approvals and explicit essential-vacancy approvals.
- Atomic, deliberate publication; immutable numbered history, amendments and selected-version Signal/PDF exports.
- Individual staff roles, mandatory TOTP/recovery, Argon2id credentials, encrypted PIN/link displays, scoped sessions, CSRF/provenance checks and persistent rate limiting.
- Single-host Docker Compose deployment, Caddy internal HTTPS, optional narrowly trusted Cloudflare Tunnel forwarding, forward-only Alembic migrations, online SQLite backup and clean quarantined restore.
- Consolidated installation, operation, upgrade, recovery and security guidance in README; retained the business-rule/acceptance contract.
- Updated cryptography to 50.0.2, pypdf to 6.21.2 and proxy brotli-libs to 1.2.1-r0 to address advisory findings; retained hash-pinned dependencies and established scheduling rules.
- Rebuilt upstream Caddy v2.11.7 with digest-pinned Go 1.27.3 and locked x/net v0.60.0, resolving CVE-2026-78669, CVE-2026-78667, CVE-2026-97031 and CVE-2026-111943 in the inspected runtime binary. Final proxy scan: no critical/high or Alpine findings; two non-applicable module matches documented in README.
- Validated 398 application and 20 browser/proxy tests, including verified TLS/HTTP2, forwarding restrictions and redirects; checked empty/IPv4/IPv6 trust configuration and existing internal CA reuse. Documented proxy-only redeployment and existing PBS recovery responsibilities.

Known limits: one app worker/host, no automatic messaging or public schedules, no multi-date/fairness optimization or downgrade/unpublish. HTML swaps accept one new rotation approval; general multi-slot/multiple-approval edits use JSON. Signal copying is manual; bundled font coverage needs actual-label review. FEASIBLE results have unfinished proof/canonicalization. Dedicated Debian performance, production Tunnel/TLS and encrypted off-host recovery/replacement-host restore require operator validation before release. This entry does not claim those checks have passed.

The Caddy security blocker is resolved locally. See README for remaining nonblocking dependency findings and unperformed operational acceptance checks; PBS remains the selected VM backup infrastructure.
