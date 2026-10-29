# Church Security Scheduler — Development Roadmap

**Current version:** v1.0.4

**Baseline:** v1.0.0 (initial private, self-hosted MVP)  
**Status:** Proposed roadmap; not an implementation specification or an approval to change existing scheduling rules  
**Maintainer intent:** Small, focused releases that preserve the established volunteer scheduling and publication safeguards

## Release strategy

- **v1.0.x — Maintenance and usability corrections:** Fix existing workflows without changing the scheduling algorithm or established business rules.
- **v1.1.0 — Usability and service-specific zone preferences:** Introduce optional inside/outside preferences early, alongside a measured modernization of the staff and volunteer interfaces. Implement preferences as a separately testable workstream before broader visual redesign.
- **v1.2.0 — Monthly planning and assignment history:** Improve visibility and planning across Sundays without silently introducing cross-date scheduling policies.
- **v1.3.0 — Fairness-aware scheduling:** Add explicitly agreed historical workload objectives after the necessary history and reporting foundation exists.
- **v1.4.0 — Communications and reminders:** Evaluate optional automated delivery and notification workflows while retaining manual distribution as a reliable fallback.

Versions and sequencing are **proposals**, not deadlines or authorized feature commitments. A feature may be split into smaller releases if necessary. Use SemVer: bug fixes in patches; added scheduling preferences and other new functionality in minor releases. No need for a v2.0.0 solely because CP-SAT objective logic changes; reserve a major version for an intentionally incompatible contract or deployment change.

## Guiding constraints for every release

1. Preserve v1.0.0 hard rules: only active, available, appropriately qualified volunteers; no duplicate assignment within a service; automatic double-service assignments rotate inside/outside; manual same-zone exceptions require the existing explicit approval process; every essential vacancy requires approval before publication.
2. Preserve essential coverage, optional coverage, double-service preference, immutability of published versions, auditability, concurrency protections, and server-side authorization. Any changed soft-objective ordering must be explicitly agreed and independently tested.
3. Do not reinterpret old availability responses, prior approvals, or published schedules. Changes to data models require forward-only migrations, upgrade/recovery instructions, and repeatable regression tests.
4. Treat preference input as a **request**, not a promise or a way to override qualifications, availability, locks, rotation, or publication approvals.
5. Maintain secure defaults, MFA, privacy-sensitive volunteer views, trusted proxy boundaries, and one-worker SQLite deployment assumptions unless a future change is intentionally scoped and approved.
6. Keep README and CHANGELOG as maintained operational documents, this roadmap as the planning document, and the v1.2 MVP specification as the **v1.0.0 baseline**. New feature design decisions should be recorded in the relevant implementation issue/PR or a compact approved amendment; do not retroactively rewrite the v1.0.0 contract to pretend a future feature already existed.
7. Preserve current Proxmox Backup Server (PBS) VM backup operations. New features must remain compatible with database-consistent backups and recovery of the original encryption key. Do not introduce another backup platform just for these features.

---

## v1.0.1 — Authentication, MFA enrollment, and staff health usability

**Classification:** Patch release; no scheduling-engine changes.  
**Goal:** Replace confusing raw browser responses with navigable, authenticated user experiences and make MFA setup easier.

**Implementation status:** Completed in v1.0.1. The changes cover A–C below. v1.1.0 and all later milestones remain proposed and unimplemented. The reviewed design below is retained as the implementation rationale, with current validation recorded at the end of that section.

### A. Redirect unauthenticated browser navigation to sign-in

**Observed behavior:** Navigating to the local origin (`https://localhost:8443`) ends up at `/staff`, where the browser displays raw JSON resembling `{"detail":"Sign in required"}` rather than a sign-in screen.

**Expected behavior:**

- An unauthenticated browser visiting `/` lands on `/login` (directly or via an appropriate redirect).
- An unauthenticated browser navigating directly to staff HTML pages, including `/staff` and staff system-health pages, is redirected to `/login`, not shown a raw JSON authorization error.
- An already authenticated staff browser visiting `/` reaches the appropriate staff landing page.
- An expired staff session follows the same browser-safe login flow without a redirect loop; after reauthentication, optional return-to behavior must not allow open redirects.
- **API/JSON clients must retain correct non-redirecting authorization semantics** (typically 401/403 JSON as applicable). Do not globally replace API auth errors with HTML redirects or weaken server-side checks.
- Volunteer link/PIN entry points remain separately scoped and unaffected. Requests for protected staff information remain inaccessible before successful authentication.

**Acceptance tests:** Root redirect in authenticated and unauthenticated states; direct deep links; expired cookie; API unauthorized responses; no open redirect; no cross-role privilege regression; Firefox/browser coverage.

### B. QR code for staff TOTP MFA enrollment

**Observed behavior:** MFA enrollment presents a manual setup secret but no scannable QR code.

**Expected behavior:**

- Display a scannable QR code during **authorized TOTP enrollment or reset**, alongside the existing manual secret as a fallback.
- Encode a standards-compatible `otpauth://totp/...` enrollment URI with the correct issuer, account label, secret, digits, period, and algorithm matching the application's verifier. Do not invent a different MFA mechanism.
- Render the QR **locally** (server-side or bundled client-side library). Never send the enrollment URI, seed, or rendered QR to an external QR generation service.
- QR and secret are available only to the staff account in its authorized, time-limited enrollment workflow; do not expose an enrolled user's seed through the normal staff UI or public routes.
- Use appropriate `Cache-Control: no-store`, safe page/referrer behavior, and no secret-containing URLs in logs, analytics, error traces, or third-party requests.
- Keep TOTP confirmation, recovery-code provisioning, and mandatory MFA behavior unchanged. The QR does not enroll an account until the existing proof-of-possession step succeeds.

**Acceptance tests:** Decode generated QR and confirm exact URI fields; enroll using a real authenticator in a manual smoke test; preserve manual entry; expired/unauthorized enrollment cannot retrieve QR; no third-party QR network requests; no secrets logged or cached.

### C. Render a staff-facing System Health page

**Observed behavior:** Navigating from the staff UI to `/staff/health` presents raw JSON fields such as `schema`, `database`, `version`, and `backup_status` rather than a styled application page.

**Expected behavior:**

- Show a normal, readable, staff-authenticated HTML health page consistent with the rest of the application.
- Present application version, database status, schema/migration status, application-backup status and last successful **application-managed** backup, where available, plus meaningful descriptions for missing/unknown data.
- Keep a stable machine-readable JSON health endpoint or preserve JSON via deliberate content negotiation. Existing monitoring/integration behavior must not break accidentally; choose and document the route/content-type contract before implementation.
- Do not imply PBS is failing merely because the application's internal backup recorder reports `never_recorded`. PBS protection is separate from application-managed backup telemetry; show a neutral explanatory label if PBS state is not integrated.
- Treat status as operational information; preserve staff-only authorization and avoid exposing keys, private paths, database contents, or unnecessary infrastructure metadata to unauthenticated visitors.
- Avoid excessive polling or new background infrastructure.

**Acceptance tests:** Authenticated browser sees HTML; unauthenticated browser redirects; existing JSON consumer still receives valid JSON under its documented interface; backup `null`/`never_recorded` renders clearly; status escape/accessibility checks.

**v1.0.1 release gate:** All existing 418 baseline tests (398 application and 20 browser/proxy, including the Caddy HTTP/2 regression) and the targeted browser/auth/QR/health tests pass; verify actual collected counts rather than freezing the future total. No change to established scheduling behavior; README/CHANGELOG updated; fresh container build and an explicitly authorized testing-deployment validation pass. Live Debian deployment requires separate authorization.

### v1.0.0 code review and v1.0.1 implementation plan

**Review baseline:** `main` commit `8685890`, locally tagged `Release-v1.0.0---Initial-MVP`. Development branch: `codex/v1.0.1-auth-health`, created from that commit. The owner subsequently authorized implementation of this v1.0.1 scope. This review describes the v1.0.0 baseline at that time.

#### Findings against the implemented baseline

| Finding | Implication for planning |
|---|---|
| All three observed usability problems match the code. `/` always redirects to `/staff`; the shared authentication error is a 401; `mfa.html` only shows the seed; `/staff/health` always returns JSON. | These are bounded presentation/response changes. No solver, database migration or authentication redesign is needed. |
| Existing staff HTML and JSON share routes. `wants_json()` checks request Content-Type/Accept; absence of JSON preference does not itself identify a browser navigation. | A global 401 redirect or treating `*/*` as HTML would change existing client behavior. Redirect only explicitly requested HTML on identified page routes; keep status codes and payloads for other callers. |
| `/staff/health` currently returns JSON even for absent Accept or `*/*`, unlike the other dual-format staff pages. | Preserve these defaults as well as explicit `application/json`. The new HTML representation must be opt-in through an explicit browser HTML Accept header. |
| MFA enrollment already has a JSON provisioning URI. It requires a password-authenticated **pending** session, not a completed staff session. Pending sessions expire after 600 seconds and are subject to active-user/credential-epoch checks. | Reuse this URI/session boundary for QR generation. Requiring a full staff session would prevent first enrollment; accepting only a user ID or a normal enrolled staff cookie would expose secrets. |
| Reset regenerates the seed, clears enrollment/recovery codes and revokes sessions. Recovery-code sign-in does not reset the seed. | Show the QR only to the reset account after its next password login, not to the resetting administrator or on ordinary MFA/recovery sign-in. No new reset flow is needed. |
| CSP is `script-src 'none'; img-src 'self'`. | External generators, JavaScript QR rendering and data-URI images do not fit the current policy. A protected same-origin SVG image fits without loosening CSP. |
| ReportLab 4.4.9 is already pinned and supports local `QrCodeWidget` plus SVG rendering; verified with a synthetic URI in the installed release image. | No new runtime QR library, image service or PNG backend is necessary. Actual QR decoding still needs a test-only decoder; Pillow alone is not a QR decoder. |
| Health uses authenticated database queries and `db.check()`. Startup also refuses a schema/key failure. It reports application `backup.completed`/`backup.failed` audit events only. | This is not an independent outage dashboard, full integrity scan, backup freshness guarantee or PBS monitor. Do not weaken authentication when the database is unavailable. |
| The roadmap's previous baseline count was 417. | Corrected to the reviewed 418; future tests add to this baseline. |

No contradiction was found between the roadmap's stated hard-rule safeguards and v1.0.0. The following later-release dependencies need explicit attention before those releases are approved; they are not v1.0.1 work:

- **Preferences:** adding an objective affects more than the CP-SAT expression. `solver.py`, worker snapshots, `generation.py` independent validation (which then required completed stages `E/O/B/U`), diagnostic/proof metadata, staleness epochs and solver-oracle tests must agree. More lexicographic stages share the existing ten-second budget and need measurement. The roadmap correctly leaves placement relative to `U` undecided. `B` counts actual double-service assignments, not simply people who said they were available for both.
- **Locks/rotation:** the promise to return a legal schedule assumes legal locked inputs. Generation currently rejects same-zone double-service locks even when a manual rotation exception was approved; preferences must not turn that refusal into an implicit waiver. Retain the existing `LOCK_CONFLICT` behavior unless separately approved.
- **History/fairness:** amendments create multiple immutable publications for one Sunday. Define which version counts and use stable volunteer IDs to avoid counting all superseded versions or grouping by display name. There is no attendance/completion/acceptance tracking, so those proposed fairness bases require additional data and decisions first.
- **Communications:** there is no delivery integration or recipient-contact/consent model. Channel support, credentials, recipient identity, delivery persistence/idempotency and revocation behavior must precede automation. Keep publication atomicity independent of external delivery availability and retain manual exports.
- **Release acceptance:** the Git tag does not establish completion of the README's Debian benchmark, real Tunnel/NAT/TLS checks, staff walkthrough or PBS replacement-host recovery rehearsal. This patch does not close those independent operational items.

#### Implemented response contract

Use explicit HTML negotiation only for the new behavior. Recognize an acceptable `text/html` media range with nonzero quality; explicit JSON preference or JSON request Content-Type wins, preserving the existing JSON convention. Absent Accept, `*/*`, and `text/html;q=0` do not opt into HTML. Do not change `wants_json()` globally. Test mixed media ranges and quality values deliberately.

| Request | Result |
|---|---|
| `GET /` | Retain the existing 303 to `/staff`. An authenticated browser lands there; an unauthenticated HTML browser then reaches `/login`. No new root API contract is introduced. |
| Eligible staff HTML page, explicit HTML, missing/expired/revoked/pending-only staff session | 303 to the fixed `/login` destination. No protected body is returned. |
| Same route with explicit JSON, absent Accept or `*/*` | Preserve existing authorization status/body; no new redirect. Explicit JSON retains JSON errors. |
| Authenticated Scheduler requesting Administrator-only page | Preserve 403; do not turn permission denial into a login loop. |
| POST/PUT/PATCH/DELETE, volunteer routes, Signal/PDF downloads, invalid Host/Origin, CSRF failure or throttling | Preserve existing handling, status, retry headers and business effects. No new auth redirect. |
| `GET /mfa`, explicit HTML, invalid/expired pending session | 303 to `/login`. JSON/default requests retain 401. Successful enrolled-user MFA still shows no seed or QR. |
| `GET /staff/health`, authenticated, explicit HTML | Render the new staff health template, `text/html`. |
| `GET /staff/health`, authenticated, explicit JSON/absent Accept/`*/*` | Preserve the existing JSON fields, types and null/`never_recorded` behavior. No endpoint rename. |
| `GET /mfa/qr.svg` | Same-origin SVG only for a valid pending session whose account is not enrolled. Missing/invalid pending session returns 401; already enrolled returns 403, with no image/seed. This asset never redirects, even if HTML is requested. |

For the redirect allowlist use matched page-route patterns, not every `/staff` prefix: `/staff`, `/staff/users`, `/staff/audit`, `/staff/health`, `/staff/settings`, `/staff/volunteers`, `/staff/volunteers/new`, `/staff/volunteers/import`, `/staff/volunteers/{volunteer_id}`, `/staff/dates`, `/staff/dates/{date_id}`, `/staff/dates/{date_id}/draft`, and `/staff/dates/{date_id}/publications/{version_id}`. Add the `/mfa` page separately. Preserve handler-level authentication in `Auth`, `VolunteerFlow`, generation and publication services; the redirect is only an error representation after authorization fails. Do not intercept unrelated 401s or any POST.

**Return-to decision for this patch:** omit optional `next`/`return_to` support. Successful MFA/recovery-code confirmation retains its existing destination and first-enrollment recovery-code screen. Fixed local redirects avoid an open-redirect surface and avoid preserving private link tokens or form bodies. Tests should confirm malicious return-to parameters are ignored. A resumable deep-link flow can be scoped separately if needed.

#### Implementation sequence and file scope

1. **Write compatibility tests first.** Extend `tests/test_auth.py` and `tests/test_request_origin.py`, with focused new test modules if readability warrants. Pin the response matrix above, explicit JSON field shapes and untouched non-page/unsafe-method behavior. Include a pending session, stale credential epoch, deactivated user, idle/absolute expiry and Scheduler/Admin distinctions. Do not weaken existing assertions merely to accommodate redirects: existing default-header 401 tests must continue to pass; add HTML-header cases alongside them.
2. **Implement page-only redirects in `scheduler/app.py`.** Add a small explicit-HTML predicate and matched-page classification shared by the existing error path. Preserve the authentication checks and exception boundaries; add `Vary: Accept` where representations differ without replacing other Vary values. Keep `Cache-Control: no-store` on every result. Do not add cookie-presence-only authentication or rewrite all errors in middleware. `/login` must remain reachable without a session, and expired `/mfa` must terminate there without a loop.
3. **Add local QR enrollment.** Reuse a single authorized enrollment-payload helper for `/mfa` and the proposed protected SVG route; do not change the current `/mfa` JSON keys or provisioning URI semantics. Render using the installed ReportLab QR/SVG facilities, with a white background, adequate quiet zone and readable responsive sizing. The QR encodes the existing PyOTP URI: issuer `Church Security Scheduler`, escaped username/account label, the existing seed, SHA-1 TOTP, six digits and 30-second period (standard URI defaults are semantically valid). Put no secret, URI or account ID in the QR route/query string, image alt text, filenames, logs or exception values. No seed/QR cache or generated static files. `scheduler/templates/mfa.html` loads the same-origin image only when enrollment is required, retains the manual secret and instructions, and confirms enrollment only through the existing CSRF-protected TOTP POST. Existing no-store/no-referrer/CSP/nosniff headers apply to both successful and denied image responses. No new JavaScript, CSP relaxation, seed regeneration on GET or reset controls.
4. **Add the health HTML representation.** Extract one health-data reader in `scheduler/app.py` and keep its JSON output unchanged. Add `scheduler/templates/health.html`, reuse `base.html` and the existing `utc` filter; adjust `scheduler/static/app.css` only as needed. Keep the existing `/staff/health` navigation link. Permit both authenticated staff roles. Show version, verified schema/readiness, latest application backup event and last successful application backup independently: a later failure must not erase an older success. Render null/`never_recorded` as “No application-managed backup recorded,” with “PBS status is not monitored here.” Do not assert daily freshness, restore readiness or PBS failure. Label readiness as a schema/connectivity check, not a full integrity check. If authorized health collection fails, return a sanitized 503 without leaking diagnostics or bypassing database-backed authorization; some outages necessarily prevent the page from being served. No polling, disk scanner, backup action or PBS API integration.
5. **Verify QR decodability and browser behavior.** Add a pinned/hash-locked QR decoder to `requirements-browser.in`/`requirements-browser.lock` only if required by the chosen decoder; evaluate Linux/Python 3.13 wheels and advisories before selection. Do not add it to runtime requirements. Have the disposable Firefox suite rasterize the SVG and decode that image, rather than merely checking the URI variable or an `<svg>` tag. Use synthetic secrets only and retain no secret-bearing screenshots/traces. Test account labels requiring escaping, exact decoded URI semantics, manual fallback, completed enrollment, normal MFA, authorized reset/re-enrollment, expired/revoked/other-account isolation, no external QR requests, and existing log-secret checks. Add browser tests for root/deep-link/expiry redirects and health HTML for both roles, neutral backup states and mobile/keyboard readability. Expiry must not gain extra lifetime from QR refreshes.
6. **Run regression/build validation.** Run both complete suites and the commands below against synthetic fixtures; report actual counts, skips and failures. Preserve solver, availability/PIN, approvals, publication, concurrency, proxy/IP/rate-limit and HTTP/2 tests unchanged. Build app/proxy before the browser image (its Dockerfile copies the local proxy image). No need to rebuild Caddy source or alter its trust configuration for these presentation fixes; the normal build reuses it.

   ```sh
   docker compose config --quiet
   docker compose build app proxy
   docker compose --profile test run --rm test pytest -q -p no:cacheprovider tests
   docker compose --profile browser-test build browser-test
   docker compose --profile browser-test run --rm browser-test pytest -q -p no:cacheprovider browser_tests
   git diff --check
   ```

7. **Prepare patch documentation and review.** Update README route/QR/health instructions, CHANGELOG and consistent application/Compose/browser-image version references to 1.0.1 as part of the implemented patch, not this plan. No schema, key, PIN or account recreation should be needed; document the absence of migrations. Review the eventual diff to ensure no scheduling-engine changes or v1.1+ fields slipped in. Obtain owner authorization before changing an existing test deployment; a real authenticator scan is a manual acceptance step requiring the owner's participation or a disposable test authenticator. Retain secure synthetic first-enrollment, manual-entry and reset coverage. Leave merging, tagging, publication and live deployment for explicit approval.

**Implementation notes:** `wants_json()` and authentication/solver services are unchanged. Health uses one underlying collector; default JSON retains all five fields and its existing error statuses (500 for a schema-check RuntimeError, 503 for operational database failure), while explicit HTML presents a sanitized 503 page. QR SVGs are generated in memory with ReportLab; browser-only `zxing-cpp==3.1.1` independently decodes Firefox-rendered pixels, including escaped labels and a 100-character Unicode label. The hash-pinned browser lock passed pip-audit 2.10.1 with no known findings; this does not prove native decoder code is vulnerability-free. No runtime dependency, environment, schema, key, PIN, proxy configuration or scheduling change was required for v1.0.1. The v1.0.1 validation used matching container and application version labels. The local upgrade verified an online database backup, protected key/CA/configuration archive, unchanged business-record/key/CA fingerprints, database integrity/publication hashes, HTTPS readiness and HTML-versus-JSON authorization behavior.

**Validation:** the complete application suite passed **460 tests**, preserving the original 398 and adding 62 targeted regressions. The complete browser/proxy suite passed **25 tests**, preserving the original 20 and adding five. Total: **485 passed, zero failed/skipped**. Compose configuration, app/proxy/browser builds and `git diff --check` passed. Test-only decoder audit found no known advisories; the runtime image has no decoder and the Caddy executable hash is unchanged. Manual acceptance still includes scanning a disposable enrollment QR with the owner's real authenticator and the unchanged operational checks in README. No claim is made that a real phone/authenticator, Debian deployment, Tunnel path or PBS recovery was exercised by these isolated tests.

---

## v1.1.0 — Service-specific inside/outside preferences and UI modernization

**Approved v1.0.3 amendment:** The owner explicitly selected v1.0.3 for configurable zone display labels, Outside Available/Rifle Qualified and `E > O > RS > RT > RP1 > B > U > canonical tie-break`. See [the feature amendment](docs/v1.0.3-qualification-zones.md); this is an exception to the normal minor-feature version policy. It does not implement or authorize the volunteer preferences below. Their proposed objective insertion points predate RS/RT/RP1 and require a fresh explicit decision against the seven-stage sequence before implementation. Merge, push, release and deployment remain separately controlled.

**Classification:** Minor feature release. Adding preference data and incorporating it into CP-SAT is a deliberate feature and objective change, not merely a cosmetic v1.0.x fix.

### A. Volunteer zone preferences (implement early in this release)

**User need:** A volunteer may prefer **inside during service 1 and outside during service 2**, or the reverse. Preferences can differ by service and may differ from another Sunday's preferences.

**Proposed initial experience (requires approval before coding):**

- During the existing PIN-verified Sunday availability workflow, provide an **optional** preference for each service: `No preference`, `Prefer inside`, or `Prefer outside`.
- Label preferences using configured service labels, not assumed service clock times.
- Preferences should be relevant only when the volunteer says they are available for that service; clarify whether to clear or retain a preference when service availability changes.
- If staff record a volunteer-confirmed availability change, they should be able to record the volunteer's stated preference with equivalent audit/concurrency protection.
- Present preferences to staff during draft generation/review, while never implying they are guaranteed placements.
- Existing responses must migrate safely with `No preference` as the backward-compatible default. Published versions remain unchanged.

**Scheduling constraints:**

- These are **soft preferences**, subordinate to all existing hard eligibility rules, service uniqueness, locked slots, and automatic inside/outside rotation.
- Do not let a preference force an essential/optional vacancy or silently reduce previously prioritized essential coverage, optional coverage, or double-service staffing.
- Existing manual rotation approvals remain the only way to waive strict rotation; a preference for the same zone in both services does **not** automatically authorize an exception.
- Because restricted-qualified positions 1 and 2 are outside, a volunteer's inside preference never makes them ineligible for a needed outside assignment.
- The solver must return a legal assignment even if no preferences can be satisfied; explain unmet preferences as advisory, not scheduling errors.
- Do not add cross-Sunday fairness logic here. Keep deterministic tie-breaking and existing proof/timeout semantics unless specifically approved to change them.

**Implementation decision gate — answer explicitly before building:**

1. **Capture scope:** Are preferences per Sunday/per service, persistent personal defaults copied into a Sunday, or both? **Recommended MVP extension:** per Sunday/per service in the existing availability response; no persistent defaults yet.
2. **Objective precedence:** At which point in the current lexicographic sequence `E > O > B > U > canonical tie-break` should preference satisfaction be optimized? **Recommended starting point:** after `E/O/B` and before the existing unlocked-assignment retention objective `U`, but this changes regeneration stability and MUST be explicitly approved before implementation. Alternatively, after `U` preserves more incumbent assignments. Codex must not silently choose.
3. **Preference scoring:** Count satisfied available-service preferences per assignment, or score two-service patterns as a unit? **Recommended simple starting point:** one equal-weight satisfaction per assigned preferred service; no hard guarantees or person-specific priority.
4. **Editing policy:** Should changing a preference increment the date input epoch, make an existing draft stale, and require explicit regeneration/refresh? **Recommended:** yes, with existing optimistic concurrency protections; define behavior after first publication consistently with staff-confirmed amendments.
5. **Conflict display:** Decide whether to show individual unmet preference indicators to all staff roles and whether any preference information appears in Signal/PDF exports. **Recommended:** internal staff UI only, not distribution exports.

**Required tests:** Qualification/availability invariants; rotation across both services; preferences aligned and opposed to rotation; competition for essential positions; locks and manual exceptions; preference vs incumbency precedence; equal-score deterministic tie resolution; `FEASIBLE`/timeout semantics; optimistic concurrency; migration of historical rows; no rewriting of publications; no leakage through volunteer/public views.

**Engineering note:** Preserve the existing OR-Tools CP-SAT approach. This likely requires additive schema/API/form changes, an explicit preference objective stage or tie-stage alteration, updated solver diagnostics, and regression tests. Estimate complexity from the repository rather than assuming a small UI-only patch.

### B. UI modernization

Modernize progressively without changing the underlying scheduling contract:

- Clearer staff dashboard and navigation, including a coherent route between roster, Sundays, drafts, publications, settings, and health.
- More readable two-service schedule layout, vacancy/essentiality markers, locked positions, approval explanations, and regeneration feedback.
- Better responsive behavior for phones/tablets used by staff and volunteers.
- Improved forms, tables, validation messages, confirmations, consistent spacing, contrast and keyboard accessibility.
- Make common administrator/scheduler edits discoverable instead of requiring knowledge of JSON endpoints for routine scenarios, where feasible.
- Optional visual themes can be considered after core clarity and accessibility are acceptable.

**Boundary:** Do not replace server-side authorization with client-only controls or introduce a heavyweight frontend rewrite without demonstrating a practical need. Maintain server-rendered/Jinja workflows unless a scoped change is justified.

**v1.1.0 release gate:** Preferences are demonstrably soft; all hard constraints and publication checks hold; migration and existing response behavior are backward compatible; mobile/browser regression passes; UI improvements do not silently modify business logic.

---

## v1.2.0 — Monthly planning and assignment history

**Goal:** Make several Sundays easier to understand and administer together.

**Candidates:**

- Monthly calendar or consolidated overview of Sundays, availability progress, drafts, outstanding approvals, and published versions.
- Readable per-volunteer assignment history and service/zone counts from existing published records.
- Navigation and bulk *review* workflows that do not imply bulk publication or automatic creation.
- Evaluate optional multi-Sunday generation only after defining what must remain independent versus coordinated.

**Decisions needed:** Whether monthly generation belongs in this release; how overlapping drafts and per-date locks/approvals interact; whether unpublished versus published assignments count in reports. Do not invent cross-week coverage/fairness rules.

**Release gate:** Existing single-Sunday operations remain valid; version history remains immutable; no accidental multi-date publication or hidden approval bypass; reporting distinguishes drafts from published schedules.

---

## v1.3.0 — Fairness-aware scheduling

**Goal:** Use historical workload data to balance assignments across weeks while preserving staffing priorities and volunteer choices.

**Candidates:**

- Assignment frequency and distribution reporting (per volunteer, service, zone, and period).
- Explicitly approved fairness scoring in CP-SAT using an agreed lookback window and counting basis.
- Clear explanation of tradeoffs when coverage, double-service preferences, inside/outside preferences, and historical fairness compete.
- Deterministic/reproducible treatment of equivalent solutions and stable publication behavior.

**Decisions needed:** Fairness period; whether to count published, completed, or accepted assignments; treatment of no-shows/unavailable volunteers; fairness precedence relative to preference satisfaction and incumbent retention; whether volunteers can opt out of certain fairness patterns.

**Release gate:** Coverage and hard constraints still dominate; fairness cannot create unapproved essential vacancies or same-zone exceptions; independent solver-oracle tests cover competing objectives; no silent retroactive scoring of historical data.

---

## v1.4.0 — Communications and reminders

**Goal:** Reduce manual administrative follow-up while retaining reliable manual exports.

**Candidates:**

- Evaluate automated Signal schedule delivery where a reliable, supportable integration exists.
- Optional availability-submission reminders and publication/assignment notifications.
- Operator-visible delivery state, bounded retries and duplicate-prevention controls.
- Opt-in/consent, administrative authorization, message preview, and secrets management appropriate to the chosen channel.

**Decisions needed:** Supported delivery channel and integration method; who receives what and when; notification opt-in/opt-out; handling of edited publications and revoked links; whether an intermediary service is acceptable.

**Release gate:** No unsolicited or duplicate messages; no PINs or MFA secrets in messages; Signal-ready copy and PDF remain functional without the integration; publishing a schedule does not implicitly deliver it unless the owner approves that behavior.

---

## Later ideas — Not committed to the roadmap

These were intentionally outside v1.0.0 and should be added only in response to demonstrated needs: volunteer accounts, volunteer-initiated swaps/substitutions, attendance tracking, email/SMS alternatives, public schedule viewing, multi-church tenancy, and multi-worker/multi-host deployment. Avoid scope expansion by default.

## Recommended Codex implementation workflow

1. **Completed v1.0.x work:** See CHANGELOG and README for the delivered features, validation results and remaining operational acceptance checks.
2. **Before v1.1.0 preferences:** Present the five explicit policy/design choices above for owner approval. Then implement preferences as an independently testable migration + UI + solver change, followed by UI modernization in a separate reviewed workstream.
3. **For each later minor release:** Confirm business decisions first; maintain isolated development branches; demonstrate preserved hard constraints and concurrency/security invariants; benchmark relevant algorithm changes; provide migration and rollback limitations; run relevant automated and manual acceptance checks.
4. **For every release:** Codex may prepare changes and test reports but must not merge, force-push, tag, create GitHub Releases, modify PBS, or deploy to the live Debian VM without explicit instruction.

**Roadmap maintenance:** Move completed items into CHANGELOG release entries and update this roadmap when priorities or decisions change. Do not treat this planning document as authorization to implement future features automatically.
