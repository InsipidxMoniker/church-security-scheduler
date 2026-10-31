# Church Security Scheduler

Self-hosted Sunday volunteer scheduling for a specific church, maintained in a **private GitHub repository**. Current version: **v1.0.4**. Production acceptance items below remain independently tracked.

This guide covers installation, operation, recovery and development. See the [release history](CHANGELOG.md), [future roadmap](ROADMAP.md), [v1.2 MVP specification for the v1.0.0 baseline](docs/church_security_scheduler_mvp_spec_v1.2.md), and [approved v1.0.3 feature amendment](docs/v1.0.3-qualification-zones.md) for changes to that baseline. The v1.0.4 publication presentation change is recorded in the changelog.

## Features and boundaries

- Two services, eight fixed positions per service, sixteen slots per Sunday.
- Roster, qualifications, search/pagination and atomic UTF-8 CSV import.
- Shared Sunday links and personal PIN verification; first-service, second-service, both or neither availability. No response is distinct from neither.
- OR-Tools generation prioritizes essential coverage (E), optional coverage (O), services with a Rifle Qualified assignment at Positions 1/2 (RS), total such assignments (RT), Rifle Qualified assignments at Position 1 (RP1), double-service assignments (B), then retained unlocked assignments (U). Equal solutions use the existing deterministic Sunday/UUID tie-break; there is no cross-week fairness score.
- Manual assignments, clearing, swaps, locks, incremental repair and explicit configuration refresh.
- Reasoned approval of exact same-zone pairs and essential vacancies, deliberate publication, immutable numbered history and amendments.
- Staff-only Signal text and PDF for selected publications; manual distribution.
- Individual Administrator/Scheduler accounts, mandatory TOTP, recovery codes, audit history, persistent throttles, online SQLite backup and quarantined restore.

Partial/empty schedules are legal. Only active, affirmatively available, qualified people can be assigned. Positions 1/2 require **Outside Available** (formerly Restricted); positions 3/8 do not. **Rifle Qualified** requires Outside Available and is a soft preference, never a staffing or publication requirement. Outside positions are 1/2/3/8 and inside positions 4/5/6/7. Administrators may rename their display labels; mapping and rotation still use the fixed internal identities. Automatic generation always switches zones for double assignments. Manual approval waives only rotation, never eligibility or per-service uniqueness. Every essential vacancy needs approval before publication, even if a candidate could fill it. The same staff member may approve and publish as separate audited actions.

There is no public schedule viewer, automatic messaging, volunteer account system, multi-church support, multi-date generation, unpublish or database downgrade. General batch editing and swaps needing two new rotation approvals use the JSON edit API; the HTML swap form accepts one new approval. Signal copy uses native select-all/copy. DejaVu supports many scripts but not every glyph; inspect actual labels and RTL wrapping before use.

## Stack and requirements

Python 3.13, FastAPI/Uvicorn (one worker), Jinja server-rendered forms and same-origin JSON, SQLAlchemy/Alembic, SQLite, OR-Tools CP-SAT, ReportLab and bundled DejaVu. No JavaScript framework/HTMX is used. Python dependencies are hash-pinned; Python/Caddy base images are digest-pinned. Existing application test dependencies remain in the app image; the QR decoder is browser-test-only. `scheduler.__version__`, FastAPI metadata and authenticated staff health report `1.0.4`.

Target: one Debian VM on Proxmox with local SSD-backed Docker volumes. Acceptance baseline: **2 vCPU / 2 GiB RAM**, a measurement target, not a proven capacity guarantee. Compose limits app to 2 CPUs/768 MiB and proxy to 128 MiB. Do not use NFS/SMB for live SQLite. Reserve several GiB for images/build cache, plus live DB/WAL, migration copies and 30 daily backups; monitor actual growth/free space.

Install Git, CA certificates, curl, Docker Engine (Linux containers), Buildx and Compose plugin using [Docker's Debian instructions](https://docs.docker.com/engine/install/debian/). Intended Compose minimum: v2.24+. Local validation uses Docker Desktop Linux Engine 29.9.2 / Compose 5.6.1 on Windows x86-64; this does not validate a fresh Debian host. No host Python is required. Commands assume Docker daemon access; otherwise prefix with `sudo`. Docker access is privileged host access.

Outbound DNS/HTTPS is needed for GitHub, registries, PyPI and the Go module proxy/checksum service during builds, and by the separate cloudflared host for its Tunnel. Browser assets need no third parties. Synchronize VM time for TOTP. HTTPS/cookies require one configured public origin. Defaults bind loopback only; remote operation needs explicit networking and TLS trust.

## Fresh Debian installation

Run commands from the checkout, stopping on failure. Key/bootstrap are one-time operations for a new installation only.

```sh
git clone https://github.com/InsipidxMoniker/church-security-scheduler.git
cd church-security-scheduler
git fetch --tags origin
git switch --detach v1.0.4
umask 077
cp .env.example .env
chmod 600 .env
mkdir -p recovery
chmod 700 recovery
```

Use private GitHub authentication without embedding tokens in the clone URL. Check out the v1.0.4 release tag for a fresh installation. Use the upgrade procedure below for an existing database.

Edit `.env`. Reusable remote Tunnel example:

```dotenv
APP_ORIGIN=https://scheduler.example.org
SITE_ADDRESS=scheduler.example.org
BIND_IP=192.0.2.20
HTTP_PORT=8080
HTTPS_PORT=8443
CLOUDFLARED_TRUSTED_PROXIES=
```

Replace example host/IP with the public hostname and VM's reachable private interface. Current deployment origin: `https://scheduler.stephenschruhl.com` (deployment-specific). `APP_ORIGIN` has no path/trailing slash; include a port only if the browser uses it. `SITE_ADDRESS` is the hostname only. Leave upstream trust empty until peer verification below. An external LXC cannot reach a loopback-only VM binding.

```sh
docker compose config --quiet
docker compose build app proxy
docker compose --profile tools run --rm volume-init
docker compose --profile tools run --rm operator init-key
docker compose --profile tools run --rm operator migrate
docker compose --profile tools run --rm operator bootstrap --username your-admin-name
docker compose up -d app proxy
docker compose ps
```

Compose creates named volumes; no host data/key directories are needed. `volume-init` sets volume roots to UID/GID 10001, mode 0700, without replacing contents. It is the only root maintenance service and has limited ownership capabilities. App/proxy use UID 10001, read-only root filesystems, dropped capabilities and bounded tmpfs. App mounts keys read-only; operator can write keys/backups.

`init-key` generates `/keys/totp.key`, refusing overwrite. `migrate` initializes SQLite; no SQL import or automatic startup migration is needed. Expected head: `0006_qualification_zones`. Bootstrap prompts privately for a 15–256-character password and refuses when any staff account exists. No default credentials or public registration/reset routes exist.

Caddy creates an internal CA on startup. Export its **public certificate only**:

```sh
docker compose cp proxy:/data/caddy/pki/authorities/local/root.crt recovery/caddy-root.crt
curl --cacert recovery/caddy-root.crt https://localhost:8443/health/ready
```

That URL applies to local defaults. For remote settings use the configured hostname/origin port, with DNS pointing to the VM (or curl `--resolve HOST:8443:VM_IP` for a direct check). Expect `{"status":"ready"}` and HTTP redirect to `APP_ORIGIN`. For direct browsers, explicitly trust the verified development CA. For Tunnel access configure LXC CA/SNI/Host below. Do not disable TLS verification. Windows Schannel may need `curl.exe --ssl-no-revoke --cacert ...` for unavailable private-CA revocation lookups; chain/hostname validation remains enabled.

Open `/login` on the configured HTTPS origin. After the password step, scan the locally generated QR with an authenticator or enter the displayed secret manually (SHA-1 TOTP, six digits, 30-second period). Enter a valid code to finish enrollment and save the ten recovery codes shown once. The QR/secret are available only during the ten-minute pending enrollment session; an authorized reset uses the same workflow at the account owner's next password login. No external QR service is contacted. Then:

1. **System configuration**: set actual IANA timezone, service labels, optional HH:MM times, and explicitly confirm all eight Essential/Optional flags. Seeds are all-essential, unconfirmed placeholders.
2. Add actual roster/qualifications; privately provision PINs as Administrator.
3. Exercise a synthetic Sunday/link/availability/draft/approval/publication in separate staff/volunteer browser contexts; check text/PDF and audit before real use.
4. Verify existing PBS coverage, application backup consistency, alerts and a clean restore rehearsal before public operation.

## Configuration

`.env` supplies Compose interpolation; app receives only environment declared in `compose.yaml`. Defaults support local review, not complete remote deployment.

| Variable | Default | Purpose |
|---|---|---|
| `APP_ORIGIN` | `https://localhost:8443` | Correct external HTTPS origin for Host/Origin checks and links |
| `SITE_ADDRESS` | `localhost` | Caddy site/certificate hostname |
| `BIND_IP` | `127.0.0.1` | Host interface for proxy ports; remote LXC needs reachable private interface |
| `HTTP_PORT`, `HTTPS_PORT` | `8080`, `8443` | Host mappings; proxy internal ports stay 8080/8443 |
| `CLOUDFLARED_TRUSTED_PROXIES` | empty | Optional space-separated exact source CIDRs; security-sensitive |
| `MAX_BODY_BYTES` | `1048576` | App/proxy request bound |
| `CSV_MAX_BYTES` | `500000` | UTF-8 batch bound; require `MAX_BODY_BYTES >= CSV_MAX_BYTES * 4 // 3 + 4096` |
| `AUTH_ACCOUNT_LIMIT`, `AUTH_ACCOUNT_WINDOW` | `5`, `900` | Failed/in-flight password/MFA attempts per account; window/block seconds |
| `AUTH_IP_LIMIT`, `AUTH_IP_WINDOW` | `30`, `300` | Password/MFA attempts per resolved IP; window seconds |
| `PIN_ACCOUNT_LIMIT`, `PIN_ACCOUNT_WINDOW` | `5`, `900` | Failed/in-flight PIN attempts per person across dates; window/block seconds |
| `PIN_IP_LIMIT`, `PIN_IP_WINDOW` | `30`, `300` | PIN attempts per resolved IP; window seconds |
| `PIN_GLOBAL_LIMIT` | `120` | PIN attempts per 60 seconds globally |
| `ROSTER_IP_LIMIT` | `60` | Public roster reads per IP per 60 seconds |

Numeric limits must be positive. Limits precede hashing; account/IP/global scopes complement one another. Counters persist as scope digests. Success releases personal/account reservations; admitted IP/global attempts remain counted. Shared NAT shares budgets; guesses can temporarily block a person. Staff can clear personal blocks with audit. Recreate containers after environment changes; `restart` does not reload Compose environment.

Container-only settings are fixed: `DATABASE_PATH=/data/scheduler.db`, `TOTP_KEY_FILE=/keys/totp.key`, app `TRUSTED_PROXIES=172.30.19.2/32`. Standalone app trust defaults empty; Compose explicitly trusts reserved Caddy. Setting these in `.env` does not override Compose. If subnet conflicts, change IPAM, Caddy static IP and app trust together. Dynamic containers use `172.30.19.128/25` within `172.30.19.0/24`.

Timezone/labels/times/essentiality, generated PIN length (8 by default; 4–12) and link lifespan (30 positive days by default) are database settings in the Administrator UI. Lifespan changes affect new links only. Custom PINs accept 4–12 ASCII digits, preserving zeros. **Four-digit PINs and encrypted recoverable PIN/shared-link displays are approved requirements.** Prefer longer random PINs; short hashes have limited offline resistance. Preserve the original key. Pre-0004 PINs remain valid but need a deliberate reset for viewing; pre-0003 links need deliberate replacement for redisplay. Migration resets neither.

## Networking, HTTPS and client IP trust

```text
Internet -> Cloudflare (public HTTPS) -> cloudflared LXC (separate host)
         -> Debian VM HTTPS:8443 -> Caddy -> app:8000 (HTTP/private Docker network)
```

Only proxy ports are published. SQLite has no network service; app has no host port. HTTP:8080 redirects. Committed Caddy uses `tls internal` on 8443, not ACME. Tunnel is optional to the application: direct deployment can trust this CA or deliberately configure valid alternative HTTPS termination. Configure existing cloudflared to route `https://VM_PRIVATE_IP:8443`, using actual site hostname:

```yaml
originRequest:
  originServerName: scheduler.example.org
  httpHostHeader: scheduler.example.org
  caPool: /etc/cloudflared/scheduler-caddy-root.crt
  noTLSVerify: false
```

Transfer only exported public root certificate to that LXC path. Never transfer Caddy signing keys. SNI/hostname validation uses `originServerName`; `httpHostHeader` must match `APP_ORIGIN` host/port. Tunnel provisioning/credentials/ingress are separate host administration. See [Cloudflare origin parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/).

Two boundaries:

1. **cloudflared -> Caddy:** upstream header trust is off by default. Only immediate TCP peers in `CLOUDFLARED_TRUSTED_PROXIES` may assert `CF-Connecting-IP`. Strict parsing uses that header alone, never arbitrary XFF fallback. With trust empty, visitors share connector/NAT throttle identity: safe fallback, not restored visitor-IP throttling.
2. **Caddy -> app:** Caddy overwrites `X-Real-IP` from `{client_ip}`, removing raw Cloudflare/XFF/Forwarded input in its proxy configuration. It may supply its own standard forwarding headers; app ignores them. Keep Uvicorn `--no-proxy-headers`. App accepts one valid `X-Real-IP` from trusted Caddy socket peer only; missing/duplicate/malformed values return 400. Other peers use socket identity. IPv6 and IPv4-mapped IPv6 normalize; scoped addresses are rejected.

Staff password/MFA, PIN and roster throttles use the same resolver. Never trust `private_ranges`, the whole LAN, all addresses or Cloudflare edge ranges for this Tunnel peer.

Before enabling visitor IPs, observe the real LXC source **inside Caddy** while its origin connection is open on Debian:

```sh
proxy_id=$(docker compose ps -q proxy)
proxy_pid=$(docker inspect -f '{{.State.Pid}}' "$proxy_id")
sudo nsenter -t "$proxy_pid" -n ss -tn '( sport = :8443 )'
```

This reads socket metadata without access logging. Configure only verified connector `/32` or `/128` CIDRs, space-separated for multiple connectors. Docker NAT may change identity; do not infer it from an LXC setting or Docker Desktop. **Never trust a shared NAT gateway unless connector source is enforced before NAT**, or preserve distinct sources first. Inspect Cloudflare Workers/Transform Rules/Pseudo IPv4 so one genuine visitor header arrives. A trusted connector/host can assert identity.

Header trust is not origin access control. Bind the intended VM interface and assess direct-origin/IPv6 reachability. Origin allowlist/firewall changes were deferred and are not implemented. If later enabled, Docker-published traffic needs forwarding-path filtering appropriate to the backend, not only UFW INPUT; see [Docker filtering](https://docs.docker.com/engine/network/packet-filtering-firewalls/). Do not widen trust for convenience.

Validate the current running proxy and recreate after changing trust; startup validates the new configuration too. The admin API is disabled, so `caddy reload` is unavailable. Do not use `compose run proxy` alongside the running proxy: both request the same reserved IP:

```sh
docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose up -d --no-deps --force-recreate proxy
```

Verify two external clients with synthetic identities and forged headers from an untrusted origin source. Inspect outcomes/hashed counters locally, without access logs or real PIN budgets. Production source/NAT/Cloudflare headers/visitor separation remain unverified by local tests.

## Administration and scheduling

Normal browser navigation to `/` or a protected staff page reaches `/login` when the staff session is missing, pending, expired or revoked. After successful MFA, `/` reaches `/staff`. This applies only to GET requests on the documented page allowlist with an explicit acceptable `text/html` header. Explicit JSON (including mixed Accept lists containing JSON) or JSON request Content-Type retains precedence; absent/wildcard Accept and zero-quality HTML retain existing non-redirecting authorization responses. Role-denied requests remain 403. Mutations, volunteer entry points and exports retain their previous behavior. Redirects always use `/login`; `next`/`return_to` are unsupported.

For explicit HTML submissions to the staff login form, incorrect credentials return **401 on the styled login page**, with a generic “Invalid username or password” alert, an escaped username and an empty password field. The retry form gets fresh preauth CSRF; no pending/authenticated session is created by a failed attempt. Form/CSRF failures retain 422/403 and throttles retain 429/Retry-After. Invalid MFA/recovery codes use the same inline pattern on the MFA page (401); throttles retain 429 and the original valid pending session. Codes are never repopulated, and an expired pending session requires password sign-in again. Explicit JSON takes precedence; absent/wildcard/disabled HTML Accept retains the existing JSON errors, including `{"detail":"Invalid credentials"}`. Other API/mutation error handling, CSRF/provenance checks, TOTP enforcement and credential throttles are unchanged.

**System health** at `/staff/health` serves an HTML snapshot for explicit HTML requests and the existing JSON fields for explicit JSON, absent Accept or `*/*`. Both Administrator and Scheduler roles may view it after MFA. It shows application version, database/schema readiness and application-managed backup events, including the last successful backup independently of a later failure. Responses use `Vary: Accept` and `Cache-Control: no-store`. No recorded application backup is a neutral unknown state; **PBS status is not monitored here**. Readiness is a connectivity/schema check, not a full integrity scan or recovery rehearsal. A database outage may prevent authentication and the page itself; HTML failures are sanitized, and existing JSON error statuses are retained.

Both staff roles manage roster, qualifications, Sundays/links, confirmed availability, drafts, approvals and publication. Only Administrators manage global settings, staff and PIN issue/reset/view. Server checks cover history/exports too. The last active Administrator cannot be removed. Audit and `/staff/health` are staff-only.

**Delete staff / volunteer:** use Delete beside the existing account controls or volunteer Edit link, review the named confirmation dialog, then select Confirm deletion. Cancel saves nothing. Delete deactivates the record, immediately revokes all its sessions and retains its identity, availability, audit records and published history. Administrators may remove other staff accounts; they cannot remove their signed-in account or the last active Administrator. Administrators and Schedulers may remove volunteers. Lists default to Active; choose Inactive (or All), edit the entry and save Active to restore it. Staff restoration retains password/MFA enrollment; volunteer restoration retains the existing PIN and submissions. Revoked sessions remain revoked, so sign in or verify the PIN again.

Inactive volunteers disappear from public name selection, active availability counts and future scheduling eligibility. Existing unpublished assignments stay visible as inactive conflicts, blocking publication. Repair or clear those assignments; unlock invalid locked assignments before generation. Incremental repair and exact-pair approval rules still apply. Published snapshots, attribution, historical names, Signal text and PDFs remain unchanged even after reactivation or renaming. A stale or duplicate removal returns 409; reload and review the current entry instead of repeating an old confirmation. Existing audit actions `staff.access_changed` and `roster.updated` record the target ID, authenticated actor, UTC event time and before/after status.

CSV header is `display_name,outside_available,rifle_qualified`, booleans `true`/`false`. Rifle Qualified requires Outside Available; invalid rows prevent the entire import. Legacy `display_name,restricted_qualified` remains accepted, explicitly identified in preview, with Rifle Qualified false. Upload UTF-8 (optional BOM) or paste, preview, then commit the whole batch. Imports create active identities, never merge names or import PINs. Duplicate active public labels need distinguishing labels. Search defaults to 50/page (API up to 100); no roster cap. The Delete action deactivates an identity and retains history. Scheduler-created volunteers need Administrator-issued PINs.

Both staff roles edit Outside Available and Rifle Qualified. To remove Outside Available from a Rifle Qualified person, first clear Rifle Qualified and save; then remove Outside Available. No qualification is silently cleared. Rifle-only changes leave legal draft assignments intact and mark earlier optimization inputs outdated; regenerate to apply the new preference. Qualification information stays off volunteer views and Signal/PDF exports.

At fixed E/O/RS/RT, **RP1** prefers the Rifle Qualified volunteer at Position 1 when only one can fill Positions 1/2 in a service. Another eligible Outside Available volunteer may fill Position 2; two rifles may fill both. RP1 is soft, subordinate to coverage/rifle distribution/total, and ahead of B/U. A standard-volunteer lock or intentional vacancy at Position 1 is binding, so the rifle can use Position 2. Earlier six-stage results are marked historical; regenerate explicitly to measure/prove RP1. This refinement needs no additional migration or version bump.

Only Administrators edit the Inside/Outside display labels in System configuration. Labels are trimmed, nonblank, at most 100 characters, free of control characters and distinct ignoring case. Renaming advances configuration/settings revisions; existing drafts require explicit refresh with a diff. Existing date timezones and published label snapshots remain unchanged. Zone labels remain available in staff drafts/configuration. Published HTML, Signal text and PDFs omit zone labels for all versions, including historical publications and amendments. Stored payloads and hashes retain their authoritative zone data.

**Issue/Reset PIN** accepts random/custom digits. **View PIN** is an audited Administrator POST that decrypts/checks the copy against the hash. Reset revokes sessions/personal block, retaining responses. Staff can redisplay the current Sunday link. Replacing/revoking it revokes old links/sessions without deleting responses. Share PINs privately, never in Signal groups, CSV, URLs or reasons.

Volunteers select their label, verify PIN and save one of four explicit choices while collection is open before first publication. Sessions last 30 idle minutes/two absolute hours, scoped to person/date/link generation/credential epoch. Others' responses, qualifications and schedules stay private. Staff enter only volunteer-confirmed responses with a reason; after publication an amendment draft is required.

1. **Sunday availability -> Open Sunday**: local Sundays only. Create/share link manually; inspect response counts. Close/reopen collection before publication only.
2. **Schedule draft -> Generate draft**: inspect all slots/proof stages. `OPTIMAL` proves E/O/RS/RT/RP1/B/U and full canonicalization. `FEASIBLE` is legal but proof/canonicalization is unfinished. A stage starts only after the previous one is proven optimal; all stages share the existing ten-second budget. A vacancy does not prove that position has no candidate. Missing rifle coverage requires no new approval.
3. Expand **Edit position N** to assign/clear/lock; use atomic swaps and search/page candidates. Invalid drafts permit conflict-reducing repairs only. Invalid locks block generation; locked same-zone pairs block even with approval. Finish manually or explicitly repair/unlock.
4. New same-zone pairs need a nonblank reason with the edit. Approve exact essential vacancies (e.g. `1:1, 2:2`) with a reason. Optional vacancies warn. Approvals bind exact revisions/fingerprints; change-and-revert never restores an invalid approval.
5. Confirm publication separately. Server rechecks eligibility/setup/slots/approvals/epochs/base in one transaction. First publication permanently closes volunteer writes/revokes sessions. On 409 reload/review. Same publication key/content retries return the original result.
6. Select a publication for Signal select-all/copy or PDF download. Reasons/approver identities stay private. Old versions show superseded warnings. **Create or resume amendment draft** for changes; confirm availability, repair, get fresh approvals and publish next version. Self-service stays closed.

Timezone-only changes affect new dates; existing Sunday timezones never change. Label/time/essentiality changes stale drafts and require reviewed **configuration refresh**. Historical payloads remain immutable.

Passwords/PINs verify with Argon2id. MFA seeds/saved PIN/link displays use Fernet and the separate key. Two concurrent credential hashes and one generation are admitted; excess work gets 429. Generation uses an isolated, single-thread solver with a shared ten-second budget outside DB transactions. Two PDF renders may run concurrently. Keep one Uvicorn worker; multiple app processes/hosts are unsupported.

Staff sessions last 30 idle minutes/eight absolute hours; pre-MFA ten minutes. Secure/HttpOnly/host-only/SameSite cookies, CSRF and configured Origin protect mutations. Firefox under `no-referrer` may omit/send null Origin: only `Sec-Fetch-Site: same-origin` plus valid CSRF permits this fallback. Foreign Origin is rejected. Privacy extensions must not strip both provenance headers.

Lost MFA: use a one-use recovery code after password (revokes older sessions), or trusted local recovery, prompting privately and resetting password/MFA/codes/sessions without replacing the key:

```sh
docker compose --profile tools run --rm operator recover --username your-admin-name
```

Normal controls:

```sh
docker compose ps
docker compose logs --tail 50 app proxy
docker compose restart app proxy
docker compose stop app proxy
docker compose up -d app proxy
```

`down` preserves volumes; **never use `down -v` on a deployment**. Readiness checks DB/schema; liveness is minimal. `unless-stopped` restarts exited processes, not a process merely marked unhealthy. Proxy waits for healthy app startup and allows 30 seconds for upstream response headers. Access logs are disabled and errors sanitized. Keep HTTP/SQL debug tracing off with real credentials/token URLs.

## Updates and rollback limits

### v1.0.3 to v1.0.4

This presentation-only patch requires no migration, configuration changes or credential resets; schema remains `0006_qualification_zones`. Published HTML, Signal text and PDF output omit zone names even for old snapshots. Stored payloads and hashes remain unchanged; internal staff drafts retain configured labels. Existing v1.0.3 code/images remain schema-compatible for rollback.

Record previous image/commit IDs and take a uniquely named verified backup before switching to the v1.0.4 release. Preserve the Compose project, volumes, original key and Caddy certificates. Rebuild and restart using the existing checkout:

```sh
git status --short
docker compose --profile tools run --rm -T operator backup --destination /backups/pre-v1.0.4-YYYYMMDDTHHMMSS.sqlite
docker compose --profile tools run --rm -T operator verify-backup --source /backups/pre-v1.0.4-YYYYMMDDTHHMMSS.sqlite
git fetch origin --tags
git switch --detach v1.0.4
docker compose config --quiet
docker compose build app proxy
docker compose up -d --wait --wait-timeout 90 app proxy
```

Verify health version 1.0.4, a published page, Signal/PDF exports and internal draft labels. Do not regenerate, republish or refresh drafts as part of this upgrade. No database migration command is needed. Never reset volumes or run `down -v`.

Historical local validation and its limits are recorded in the [changelog](CHANGELOG.md#v104--hide-zone-labels-in-finalized-schedules).

Deploy reviewed tags, preserving checkout path, Compose project, `.env`, volumes and original key. Default volume names start `church-security-scheduler_`; changing projects creates different volumes and can look like lost data. Never bootstrap/init-key for upgrades.

### v1.0.2 to v1.0.3

This upgrade requires migration **0006**, with no dependency, key, account, PIN or environment changes. See the [feature amendment](docs/v1.0.3-qualification-zones.md) for schema details, test evidence and remaining operational gates. The following operator instructions use Debian/sh. Production deployment validation remains a separate operational task.

First, while still using the previous checkout and matching application image, record commit/image IDs, protect the original key/config/CA recovery material, stop writes, and take a verified backup. The old verifier must be used before switching images: the v1.0.3 verifier intentionally rejects old schema 0005.

```sh
git status --short
git rev-parse HEAD
docker compose images
docker compose stop proxy app
docker compose --profile tools run --rm -T operator backup --destination /backups/pre-v1.0.3-YYYYMMDDTHHMMSS.sqlite
docker compose --profile tools run --rm -T operator verify-backup --source /backups/pre-v1.0.3-YYYYMMDDTHHMMSS.sqlite
```

Replace the timestamp consistently with a unique value and stop on any failure. Export/protect that verified backup and original key/config under the existing recovery process before migrating. Keep enough database-volume space for an additional pre-migration SQLite copy. Then check out the v1.0.3 release, preserving the same project and volumes:

```sh
git fetch origin --tags
git switch --detach v1.0.3
docker compose config --quiet
docker compose build app proxy
docker compose --profile tools run --rm -T operator migrate
docker compose --profile tools run --rm -T operator verify-backup --source /data/scheduler.db
docker compose up -d --wait --wait-timeout 90 app proxy
docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose ps
```

Verify CA-validated readiness and authenticated health (`1.0.3`, `0006_qualification_zones`), existing MFA/PINs/availability, old publication hashes/text/PDF, and matching Outside Available values with every existing Rifle Qualified false. Review each old draft's explicit configuration diff/refresh; do not regenerate or publish silently. Exercise label editing and qualification dependency with disposable data. The migration itself preserves all slots, locks, approvals, credentials, UUIDs, audit and publication rows. Stale drafts keep their assignments visible; existing timezone snapshots stay fixed.

On failure keep public traffic stopped and preserve the failed state and original backup. **Do not run previous images against schema 0006 or attempt an Alembic downgrade.** To roll back, use the recorded previous code/image and verified pre-migration backup plus original key/config in a clean isolated destination through the matching previous release's quarantined restore procedure below. To recover into v1.0.3, migrate an offline copy of the old backup before current-head verification/restore; preserve the original. Both upgrade preservation and the offline-copy/current restore path are covered by automated tests. Restore revokes sessions/links and closes collection; post-backup changes are lost. Never overwrite a live destination, reset a database/key, delete WAL/SHM, or use `down -v`.

### v1.0.1 to v1.0.2

**v1.0.2 - Staff and Volunteer Removal.** No schema migration, new dependency, environment change, PIN reset or credential recreation is required; schema head remains `0005_publication_guards`. The solver and proxy configuration are unchanged. Application, proxy and browser image labels advance to 1.0.2.

Record the previous commit/images and use a clean deployment checkout. Stop on failure, keep the existing Compose project/volumes/key and choose unique backup filenames:

```sh
git status --short
git fetch origin --tags
git switch --detach v1.0.2---Staff-and-Volunteer-Removal
docker compose config --quiet
docker compose build app proxy
docker compose --profile tools run --rm -T operator backup --destination /backups/pre-v1.0.2-YYYYMMDDTHHMMSS.sqlite
docker compose --profile tools run --rm -T operator verify-backup --source /backups/pre-v1.0.2-YYYYMMDDTHHMMSS.sqlite
docker compose up -d --wait --wait-timeout 90 app proxy
docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose ps
```

Check CA-verified readiness and health version 1.0.2, then use disposable identities to verify removal, sign-in/PIN denial, Inactive filtering and restoration. Check a draft conflict and an existing publication/export before accepting the update. This checkout does not deploy automatically. Recorded v1.0.1 images remain schema-compatible for rollback; deactivated status and revoked sessions persist in the database. Keep the backup and original key; do not restore over a running database.

### v1.0.0 to v1.0.1

This patch needs container rebuild/recreation, **no migration**, no environment changes and no new runtime dependencies. Schema remains `0005_publication_guards`; retain the original key, accounts, PINs, sessions and Caddy volumes. Caddy source/build/configuration is unchanged; its local image label follows the application release. The independent QR decoder is installed only in the browser test image.

Run on Debian/sh from the existing deployment checkout, stopping on failure. Choose a unique backup filename in place of the timestamp placeholder:

```sh
git status --short
git fetch origin --tags
git switch --detach Release-v1.0.1---Redirects,-MFA-QR,-Health-Page
docker compose config --quiet
docker compose build app proxy
docker compose --profile tools run --rm -T operator backup --destination /backups/pre-v1.0.1-YYYYMMDDTHHMMSS.sqlite
docker compose --profile tools run --rm -T operator verify-backup --source /backups/pre-v1.0.1-YYYYMMDDTHHMMSS.sqlite
docker compose up -d --wait --wait-timeout 90 app proxy
docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose ps
curl --fail --cacert recovery/caddy-root.crt https://localhost:8443/health/ready
```

Use the configured site hostname/port or direct-origin `--resolve` for remote deployments. Verify existing staff MFA, browser redirects, health HTML/JSON, and a disposable account's QR/manual enrollment before accepting the update. Deactivate the disposable account afterward. Production deployment requires owner approval. With this unchanged schema, recorded v1.0.0 code/images remain compatible with the same data if an application rollback is needed; retain the recovery backup and follow the general rollback limits below.

### General updates that include migrations

1. Confirm clean checkout, record previous tag/commit, and take verified backup plus protected off-host key/config copy below. For legacy schema, use the old matching release's tool or supported SQLite API; this verifier requires its own head.
2. Fetch/build before downtime (replace the tag with the reviewed target release):

   ```sh
   git status --short
   git fetch origin --tags
   git switch --detach v1.0.4
   docker compose config --quiet
   docker compose build app proxy
   ```

3. Stop writes and migrate:

   ```sh
   docker compose stop proxy app
   docker compose --profile tools run --rm volume-init
   docker compose --profile tools run --rm operator migrate
   docker compose --profile tools run --rm operator verify-backup --source /data/scheduler.db
   docker compose up -d app proxy
   docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
   docker compose ps
   ```

4. Check authenticated health version/schema, CA-validated readiness/login, existing MFA/roster/responses/history/text/PDF, then public access/synthetic availability. Retain recovery material until checks succeed.

`migrate` makes a consistent pre-migration SQLite API copy beside DB (`scheduler.db.pre-migration-TIMESTAMP.sqlite`, 0600). These consume database volume space and are not off-host backups. Startup rejects schema/key failures. Never rewrite deployed migrations; add revisions. Revision 0005 adds publication/pointer triggers, without table rebuild. Downgrades are disabled.

On failure keep public traffic stopped. With unchanged compatible schema, rebuild previous recorded code/image. Otherwise preserve failed state and restore the **matching previous DB, key, config and code** into a clean environment. Changes after backup are lost and sessions/links quarantined. There is no automatic/in-place rollback or unpublish. Never delete WAL/SHM or run old code against new schema.

Historical recovery locator retained from October 24 local upgrade: project `church-security-scheduler`, volume `church-security-scheduler_backups`, `/backups/pre-phase4-20261024T164715754104Z/` contains `scheduler.sqlite` (schema 0004) and protected `keys.tar`. This is local legacy material, not encrypted off-host/current-head backup; inspect with matching code, never overwrite the running DB with it.

### Proxy-only security update

After checking out the reviewed release on Debian, keep the same project, `.env` and volumes. Stop on any command failure. Build before the brief proxy restart; the app stays running. No migration, key generation, account/PIN reset, Tunnel change or CA replacement is required:

```sh
docker compose config --quiet
docker compose build proxy
docker run --rm --network none --entrypoint caddy church-security-scheduler-proxy:1.0.4 build-info
docker run --rm --network none --entrypoint sha256sum church-security-scheduler-proxy:1.0.4 /usr/bin/caddy
docker compose up -d --no-deps --force-recreate proxy
docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose exec -T proxy sha256sum /usr/bin/caddy
docker compose ps
docker compose logs --tail 50 proxy
```

Confirm the running binary hash equals the built/scanned image's hash, and check CA-verified readiness/login through the configured hostname. For the current Tunnel deployment, replace `VM_PRIVATE_IP` below; use the existing verified public CA certificate:

```sh
curl --fail --cacert recovery/caddy-root.crt \
  --resolve scheduler.stephenschruhl.com:8443:VM_PRIVATE_IP \
  https://scheduler.stephenschruhl.com:8443/health/ready
```

Also verify the public Tunnel origin and a synthetic login/availability workflow. Do not use `down -v` or start a second proxy with the reserved IP. Existing `/data` and `/config` retain the internal CA. The local upgrade rehearsal preserved all existing CA certificate/key bytes and verified HTTPS; actual Debian/Tunnel verification remains an owner task. Scan any independently rebuilt deployment image before accepting it.

## Backup and clean restore

**Proxmox Backup Server (PBS) is the existing VM-level backup/recovery platform.** No additional backup service is required. Application-consistent SQLite copies below remain useful for granular recovery. Complete recovery needs the database, persistent Docker volumes (including Caddy CA state), application configuration and original encryption key. The owner must verify PBS job coverage, backup consistency, retention and replacement-host restoration; PBS coverage alone does not establish an application-consistent restore, and no PBS recovery rehearsal has been verified here.

Database volume: `/data/scheduler.db`, WAL/SHM and migration copies. Key volume: `/keys/totp.key`. Operator backups: `/backups`. Caddy volumes hold CA/private signing keys/TLS state (`/data`) and configuration state (`/config`); Caddyfile is bound read-only from checkout. Protect DB/key separately: both together recover saved MFA/PIN/link secrets.

Daily host/systemd job and before upgrades, using unique names:

```sh
docker compose --profile tools run --rm -T operator backup --destination /backups/scheduler-YYYY-MM-DD.sqlite
docker compose --profile tools run --rm -T operator verify-backup --source /backups/scheduler-YYYY-MM-DD.sqlite
```

SQLite online backup API permits concurrent writes. It verifies integrity/FKs/schema/publication hashes, fsyncs a staged file, then publishes atomically without overwrite. Never copy live `.db` alone: commits may be in WAL. Interrupted temporary backups are not complete. Destination parents must exist.

For granular recovery, export verified DB and original key into protected staging on **Debian/sh** and protect it under the existing backup process:

```sh
umask 077
mkdir -p recovery
chmod 700 recovery
docker compose --profile tools run --rm -T --entrypoint cat operator /backups/scheduler-YYYY-MM-DD.sqlite > recovery/scheduler-YYYY-MM-DD.sqlite
docker compose --profile tools run --rm -T --entrypoint tar operator -C /keys -cf - totp.key > recovery/keys.tar
cp .env compose.yaml recovery/
cp deploy/Caddyfile recovery/Caddyfile
chmod 600 recovery/*
```

Stop on export failure. Binary redirection requires a binary-safe shell/tool; older Windows PowerShell is unsuitable. Save tag/commit, image IDs, schema and this guide with configuration. Staging is sensitive and ignored by Git/build. Keep key recovery separately protected, encrypt off-host, verify transfer/decryption and use files 0600/directories 0700. Back up Caddy CA state with a trusted volume backup tool if continuity matters; otherwise redistribute a new public CA to connector/clients on restore. Public root.crt is not a private-key backup.

Retain newest **30 verified daily application copies** by default; PBS retention is separately owner-managed. Prune old application backups only after a verified protected off-host copy, never live database/key volumes. Scheduling/encryption/transfer/retention are host responsibilities. Monitor disk and `/staff/health` last-success/latest-failure; alert on failure/no daily success. Full disk may prevent a failure audit. Daily backups can lose changes since the last copy.

Restore on a clean, inaccessible replacement host using matching release/schema, never an existing production volume. Same-host rehearsal needs a different project **and overrides for subnet, Caddy static IP, app trust and host ports**; project name alone does not avoid fixed-subnet overlap.

1. Clone matching release, restore reviewed `.env`/Caddyfile/config, build images, run `volume-init`, keep app/proxy stopped. Decrypt verified recovery files into private `recovery/`. No bootstrap/init-key.
2. Import key into empty key volume, restore into empty DB:

   ```sh
   docker compose --profile tools run --rm -T -v "$PWD/recovery:/recovery:ro" --entrypoint sh operator -c 'test ! -e /keys/totp.key && tar -xf /recovery/keys.tar -C /keys totp.key && chmod 600 /keys/totp.key'
   docker compose --profile tools run --rm -T -v "$PWD/recovery:/recovery:ro" operator restore --source /recovery/scheduler-YYYY-MM-DD.sqlite --destination /data/scheduler.db
   docker compose --profile tools run --rm -T operator verify-backup --source /data/scheduler.db
   ```

3. Restore refuses existing DB/WAL/SHM, verifies integrity/FKs/schema/hashes, decrypts staff seeds with recovered key, then publishes staged DB. It revokes all staff/volunteer sessions/old public links, removes link displays, closes collection and advances date epochs. Publications stay immutable. Wrong key exposes no restored destination. Legacy backups need supported offline migration on a copy before current-head verification.
4. Start behind operator-only listener. Check readiness, password/MFA, backup age, publication numbers/hashes/text/PDF and audit. Exercise recovery code on synthetic account; old cookies/links must fail. Without restored Caddy state, install new public CA on cloudflared and verify chain/hostname.
5. Issue fresh links/reopen collection deliberately for unpublished Sundays and replace Signal links manually. Published Sundays stay closed. Reopen public ingress after checks; record recovery time and rehearse periodically.

## Troubleshooting

| Symptom | Check/action |
|---|---|
| App exits/unhealthy | Logs; original key, schema head, positive limits. Migrate only stopped with recovery copies secured. |
| Volume permission denied | `volume-init`; UID 10001, rootless/user-namespace mappings, disk space. Never chmod world-writable. |
| Invalid/ignored environment | Compare `.env` to Compose interpolation; `config --quiet` checks syntax, startup checks limits. Recreate services. |
| 400 Host / 403 same-origin | Exact external `APP_ORIGIN` host/port and cloudflared Host; fresh login CSRF form; preserve Origin/Fetch Metadata. |
| Caddy/Tunnel TLS/502 | App health, DNS, LXC-to-VM:8443, CA, SNI, Host. Validate Caddy; do not bypass TLS. |
| Shared/wrong client IP, 429 | Empty trust shares connector budget. Measure real peer/NAT/Cloudflare headers; use exact verified CIDRs only. Respect Retry-After. |
| Migration/schema failure | Keep stopped; inspect error/disk/permissions/backup. Forward migrate or matching-code clean restore; no DB reset. |
| 409 stale edit/publication | Reload/review current inputs, approvals/configuration; do not reset epochs. |
| LOCK_CONFLICT / 503 solve | Repair/unlock invalid/same-zone locks. No incumbent/worker failure preserves draft; retry deliberately. |
| Database busy/full | Bounded 503/Retry-After; inspect writer load/disk, keep one worker. Never delete WAL or edit revisions. |
| Lost MFA/PIN/link | Recovery code/local staff recover; Administrator PIN view/reset; staff link replacement. Never replace encryption key. |

## Development and release validation

`scheduler/` holds services/templates/static/font; `migrations/` immutable forward schema; `deploy/` Caddy/browser builds; `tests/` and `browser_tests/` synthetic regressions. Existing phase-numbered module/test/migration filenames are retained to avoid gratuitous import/migration changes. No CI workflow exists; run both suites before release.

```sh
docker compose build app proxy
docker compose --profile test run --rm test pytest -q -p no:cacheprovider tests
docker compose --profile browser-test build browser-test
docker compose --profile browser-test run --rm browser-test pytest -q -p no:cacheprovider browser_tests
docker compose config --quiet
docker compose exec -T proxy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
git diff --check
```

Tests mount no deployment data/key volumes; app fixtures migrate temporary on-disk SQLite. Browser tests run real HTTPS Caddy/Uvicorn/Firefox with disposable CA/key/DB. HTTP integration validates CA; isolated Firefox context bypasses its disposable CA only. Coverage includes independent solver oracle/canonical vectors, generated constraints, stale/competing operations, auth/PIN/CSRF/roles, subprocess failure, approval integrity, persistence/restore/wrong-key, proxy spoofing and throttle separation. Warnings are errors except two named library deprecations. Artifacts belong in ignored `test-results/`, not permanent phase reports.

Historical test counts and local validation results are recorded by version in the [changelog](CHANGELOG.md). The [v1.0.3 amendment](docs/v1.0.3-qualification-zones.md#validation-evidence) retains benchmark scope, reproduction and limitations. These historical results do not establish current production acceptance.

Maintain locks with disposable Python 3.13, compatible pip 25.2/pip-tools 7.5.3. Preserve pins unless deliberately upgrading; regenerate hashes and rerun both suites:

```sh
pip-compile --generate-hashes --output-file=requirements.lock requirements.in
pip-compile --constraint=requirements.lock --generate-hashes --output-file=requirements-browser.lock requirements-browser.in
```

Use `--upgrade-package NAME` for targeted upgrades. Audit both locks with `pip-audit`, assessing actual used APIs. A clean scan/tests do not prove absence of vulnerabilities. Review Python/Caddy/Debian/Alpine updates; full OS/browser image scanning is separate from Python auditing. Optional `TEST_RESULTS_DIR` in the test container can retain growth JSON in an explicitly mounted writable directory; the default uses pytest temporary storage.

Production release gates remain: specification §15 dedicated Debian benchmark (20 generation runs, 100 mixed HTTP operations, 20 concurrent volunteers/two staff, p95, proof stages, RSS); actual Tunnel TLS/source/NAT/spoofing checks; encrypted off-host key/config recovery and replacement-host restore; nontechnical staff/phone acceptance and actual font coverage. Functional local growth tests do not establish these measurements. Keep measured evidence outside this maintained guide and report the release verdict separately. Candidate version numbers imply no production validation.

## Dependency review and release acceptance

Review baseline: October 24, 2026. Both application and browser Python locks passed pip-audit after targeted cryptography 50.0.2 / pypdf 6.21.2 updates. Only those two locked package versions changed. The application uses Fernet, not the cryptography certificate/PKCS7 APIs reported by the original advisories. pypdf is used by export tests, not an untrusted-PDF ingestion endpoint. This narrows exposure; the updates still remove known dependency findings.

### Maintained Caddy build

At review time the latest official stable Caddy release/image was still **2.11.7**, with Go 1.26.8 and x/net v0.59.0 compiled into its executable. `deploy/Dockerfile.caddy` therefore builds vetted upstream Caddy **v2.11.7** using digest-pinned official **Go 1.27.3** and the checked-in `deploy/caddy/go.mod`/`go.sum`. The standard upstream entry point adds no plugins. The only compiled module version override is **golang.org/x/net v0.60.0** for the HTTP/2 fix; all other 134 compiled module versions match the previous binary. This is a deliberate local compatibility responsibility, covered by the full proxy suite.

Module checksums are verified; `GOTOOLCHAIN=local` and `-mod=readonly` prevent implicit toolchain/graph changes. Static builds retain upstream `nobadger,nomysql,nopgx` tags and use `-trimpath`, no VCS stamping and an empty build ID. A repeat compile with network access disabled reproduced the binary byte-for-byte. The runtime remains the digest-pinned official Caddy Alpine image (Alpine 3.23.7), with patched Brotli 1.2.1-r0 and only `/usr/bin/caddy` replaced. Builder/compiler/cache stay outside the runtime. UID 10001, CA paths, TLS/HTTP2, trust boundaries and Compose hardening are preserved. Return to an official image by removing the builder/binary replacement once its **actual executable** has verified patched dependencies; retain an appropriate patched runtime/Brotli package and repeat validation.

Inspection of the final runtime executable confirmed Caddy v2.11.7, **Go 1.27.3**, **x/net v0.60.0** and SHA-256 `3a890f85693615efacf52466220371f6420496c75d51a4b397757073a3efd66c`. The v1.0.0 remediation's local linux/amd64 image index was `sha256:0318068db727635c7c752e2baf4efb0f12f815d3bcd7ce68a3af4783fa83329a`; build attestations/platforms and release labels can produce a different image digest. v1.0.1 retains the same binary/runtime build. Inspect and scan each deployment's actual artifact too.

Trivy's all-severity scan of this image found **zero critical/high findings and zero Alpine findings**, with the two module matches assessed below. Binary-mode govulncheck reported the OpenPGP module advisory; source-mode analysis of the exact locked build and tags found **zero reachable vulnerabilities, zero affected imported packages**, and that one module-only advisory. This is evidence about the current build, not a claim that the whole application image is vulnerability-free. The earlier high-only scan did not inventory lower-severity findings.

| Finding | Assessment and disposition |
|---|---|
| Caddy compiled dependencies: CVE-2026-78669, CVE-2026-78667, CVE-2026-97031, CVE-2026-111943 | **Resolved:** actual runtime Go 1.27.3 and x/net v0.60.0 meet the fixed versions. All four advisories are absent from the final image scan (previously five high package/advisory matches). HTTP/2 remains enabled and tested. Alpine package updates alone would not fix these compiled dependencies. |
| Alpine brotli-libs 1.2.0-r0, CVE-2026-111497 | Resolved locally: proxy Dockerfile pins 1.2.1-r0. No application Brotli decoding path was identified. |
| chi v5.3.2, CVE-2026-114437 (medium; fix v5.4.0) | Trivy module match; affected `ClientIPFromXFF` middleware is unused by the Caddy/Smallstep sources in this build. Caddy uses its own explicitly configured strict client-IP parser; spoofing regressions pass. Non-applicable to current routing; reassess before adding chi middleware. Module version was not changed by this remediation. |
| x/crypto v0.57.0, GO-2026-5932 (unknown; no fix) | OpenPGP packages are unmaintained. Binary analysis flags the containing module; exact source call-graph analysis finds no affected imported package or call. Non-applicable to this build; no OpenPGP functionality is enabled. Reassess when changing modules. |
| pip-vendored msgpack 1.1.2, setuptools 70.3.0, urllib3 2.7.0 | Scanner finds GHSA-6v7p-g79w-8964, CVE-2025-47273, CVE-2026-97687 and CVE-2026-97689 in bundled installer tooling, outside requirements.lock. These are not imported by the application and no runtime package-install endpoint exists. Update bundled tooling with a reviewed upstream Python image/toolchain; retain as nonblocking maintenance findings. |
| Debian SQLite 3.40.1: CVE-2025-7458, CVE-2026-11822/11824 | Residual base-image findings. The first requires attacker-controlled SQL; the others require crafted FTS5 data/queries. The app uses parameterized fixed SQL and no FTS5 tables/MATCH queries or public database import. Treat as nonblocking for this implemented interface; use only trusted backups and track Debian fixes. |
| Debian zlib CVE-2023-45853 | Not applicable to the installed binary: Debian documents that affected contrib/minizip code is not built into this bookworm zlib package. Do not equate the scanner's source-package match with an exploitable application path. |
| Other Debian utility/library findings | Scanner reports no fixed bookworm versions for remaining high findings in util-linux family, ncurses, gzip, ACL, systemd libraries, OpenSSL DTLS and Perl archive tooling. No corresponding application utility, untrusted archive/terminal/partition or DTLS endpoint was identified; the app is non-root with all capabilities dropped. Track as residual base-image risk rather than claiming they are fixed. |

Primary references: [Go HTTP/2 advisory](https://pkg.go.dev/vuln/GO-2026-6611), [Go Range advisory](https://pkg.go.dev/vuln/GO-2026-6609), [Go TLS advisory](https://pkg.go.dev/vuln/GO-2026-6607), [Go certificate verification advisory](https://pkg.go.dev/vuln/GO-2026-6809), [chi advisory](https://github.com/advisories/GHSA-j6qf-3m9c-vwj3), [OpenPGP advisory](https://pkg.go.dev/vuln/GO-2026-5932), [Debian SQLite SQL advisory](https://security-tracker.debian.org/tracker/CVE-2025-7458), [Debian FTS5 advisory](https://security-tracker.debian.org/tracker/CVE-2026-11822), [Debian zlib disposition](https://security-tracker.debian.org/tracker/CVE-2023-45853). Package matching is not a penetration test; reassess reachability whenever features/dependencies change.

No identified Caddy software security blocker remains. The dedicated Debian benchmark, real Tunnel TLS/source/NAT identity, verified PBS coverage/application-consistent recovery/replacement-host restore and staff acceptance remain independent release acceptance tasks. No production data, networking or backup infrastructure was changed by the local review.
