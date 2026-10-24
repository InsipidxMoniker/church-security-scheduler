# Changelog

## v1.0.1 — Authentication and health usability (unreleased)

- Redirect explicit-HTML navigation with invalid staff sessions to sign-in on the page allowlist; preserve JSON/default authorization, role restrictions and mutation behavior. No return-to redirects.
- Add a private same-origin enrollment QR using existing ReportLab and the existing pending MFA session/URI, with manual-secret fallback and unchanged TOTP/recovery confirmation.
- Render staff System Health HTML while preserving the existing default/explicit JSON contract, backup timestamps and safe errors. Distinguish application-managed backup records from unmonitored PBS status.
- Add independent QR decoding and authentication/health browser regressions. No schema, runtime dependency, environment, scheduling, Caddy or encryption-key changes; rebuild containers to update.
- Validated 460 application and 25 browser/proxy tests (485 total, zero failures/skips), complete container builds, Compose configuration and the browser-only decoder dependency audit. Real authenticator/manual acceptance remains pending; no production deployment was performed.

### Upgrade from v1.0.0

Keep the existing checkout path, Compose project, `.env`, database/key volumes and Caddy CA volumes. This patch requires **no database migration, environment changes, key generation, administrator recreation or PIN reset**. Stop on any command failure; never use `docker compose down -v`.

For a released deployment, first record the previous commit/image IDs and secure the original key/configuration under the existing backup process. After owner approval and creation of the v1.0.1 tag, use a clean checkout:

```sh
git status --short
git fetch origin --tags
git switch --detach v1.0.1
```

For local pre-release testing, remain on the reviewed `codex/v1.0.1-auth-health` working tree instead; do not switch away from uncommitted changes or require a release tag.

Build before restarting. Replace `YYYYMMDDTHHMMSS` with a unique timestamp in both backup commands; the backup tool refuses to overwrite an existing file:

```sh
docker compose config --quiet
docker compose build app proxy
docker compose --profile tools run --rm -T operator backup --destination /backups/pre-v1.0.1-YYYYMMDDTHHMMSS.sqlite
docker compose --profile tools run --rm -T operator verify-backup --source /backups/pre-v1.0.1-YYYYMMDDTHHMMSS.sqlite
docker compose up -d --wait --wait-timeout 90 app proxy
docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose ps
docker compose logs --tail 30 app proxy
```

Check CA-verified `/health/ready` using the configured hostname, then visit `/` in a signed-out browser and confirm it reaches `/login`. Sign in with existing credentials and check **System health**: browser HTML should show version 1.0.1; authenticated requests with `Accept: application/json` retain the JSON interface. Application backup events describe application-managed copies, not PBS jobs.

The enrollment QR appears only during first enrollment or authorized re-enrollment, not normal MFA sign-in. To test it without resetting an established account, create a disposable staff account through **Manage staff**, sign in as that account in a separate browser context, scan the QR (or enter its manual secret), and verify recovery-code presentation. Deactivate that test account afterward. No test account is created automatically by an upgrade.

For this unchanged schema, the recorded v1.0.0 code/images remain compatible with existing data if rollback is needed. Preserve failed-state evidence and the recovery backup; follow the [README upgrade and rollback guidance](README.md#updates-and-rollback-limits). Do not restore over a running database or replace the original key.

## v1.0.0 — Initial release

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
