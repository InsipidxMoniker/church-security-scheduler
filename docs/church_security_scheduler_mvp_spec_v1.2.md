# Church Security Volunteer Scheduler
## Implementation specification and acceptance plan

Version: 1.2, October 23, 2026  
Status: finalized implementation contract, subject to setup and release validation below.  
Supersedes: v1.1 (and v1.0). Companion: `church_security_scheduler_technical_review.md`.

Normative language: MUST is required for MVP; MAY is optional. Explicit technical defaults below resolve implementation choices, not new church staffing policies. Where this document clarifies or supersedes v1.0/v1.1, use this document. No actual implementation has been validated by this specification.

## 1. Product and boundaries

A self-hosted, mobile-friendly application replaces manual Sunday security scheduling: collect availability using a shared Signal link and personal PIN, generate a partial or complete draft, edit and approve the limited exceptions, publish deliberately, then copy text into Signal or download PDF.

Preserve these operating rules:

- Two services, eight positions per service, exactly 16 schedule slots per Sunday.
- Volunteers select first service, second service, both, or neither. No response is distinct from neither.
- Prefer assigning both services to people who offered both, after maximizing essential and optional coverage. Among otherwise equal solutions, use a deterministic Sunday-specific volunteer ordering, not cross-week fairness scoring.
- Automatic scheduling switches zones for anyone assigned twice. Only a recorded manual same-zone exception can waive rotation.
- Positions 1 and 2 require the same restricted qualification. No exception can waive qualifications or availability.
- Essential vacancies require explicit approval, even when a person could be assigned; optional vacancies do not. Both remain visible.
- Volunteers can amend availability until first publication only. Staff handle later confirmed changes through amendments.
- Individual staff accounts, versioned publications, immutable historical outputs, audit history, and manual distribution.

Initial roster is approximately 13–20 people, currently with up to four restricted-qualified people. These are descriptive counts, never validation limits. The roster MUST grow without schema or architectural changes. Test at least 100 active volunteers across a monthly dataset, as specified in §15.

MVP includes roster CRUD and minimal CSV import, qualification administration, PIN provisioning/reset, date/link management, availability collection, single-Sunday generation, manual edits/swaps/locks, exceptions/approvals, publication/amendment/history, text/PDF outputs, and deployment/backup procedures.

Out of scope: multi-date generation in one operation; monthly workflow UI; cross-date constraints; fairness or workload scoring; attendance; volunteer accounts; volunteer-initiated substitution; SMS/email; automatic Signal actions; multiple churches; distributed infrastructure. A monthly workload means several independent Sunday records and operations, not a new feature.

## 2. Roles and authorization

| Operation | Volunteer session | Scheduler | Administrator |
|---|---|---|---|
| Read/write own availability for its authorized date before first publication | Yes | Staff workflow | Staff workflow |
| See other availability, drafts, published versions, exports, audit | No | Yes | Yes |
| Open Sundays; manage links; generate/edit/lock; approve permitted exceptions/vacancies; publish/amend | No | Yes | Yes |
| Create/edit/deactivate roster and change restricted eligibility | No | Yes | Yes |
| Issue/reset volunteer PIN | No | No | Yes |
| Manage staff credentials/roles/MFA and global position/service settings | No | No | Yes |

All authorization is server-side, including read routes and exports. A Scheduler-created volunteer needs an Administrator-provisioned PIN before volunteer verification; no automatic availability is inferred. A Scheduler can record an availability response only after confirmation from that volunteer, with an audit reason. The same authenticated Scheduler or Administrator MAY approve a rotation exception and/or essential vacancy and then publish that schedule. A second person is not required; each approval and publication MUST still be separate auditable actions with the authorizing identity and required reason. No staff role can override the non-overridable rules.

No public schedule viewer is included. Signal text/PDF distribution occurs outside the application. A shared date link exposes only date/service labels, active display names, and the authenticated person's own response.

## 3. Position and setup data

| Position | Zone | Qualification |
|---|---|---|
| 1 | outside | restricted_1_2 |
| 2 | outside | restricted_1_2 |
| 3 | outside | none |
| 4 | inside | none |
| 5 | inside | none |
| 6 | inside | none |
| 7 | inside | none |
| 8 | outside | none |

Numbers, zones, qualification mapping, and service numbers 1/2 are fixed in MVP. Administrators configure essentiality for each position, applying to both services, and explicitly confirm the full list. Seed all essential as a conservative placeholder. A setup banner and publication guard remain until confirmation. Later changes do not rewrite historical schedules. No priority ranking among essential positions is introduced.

Require church IANA timezone and service labels at setup; start times are optional. Never invent actual service times. Dates are local Sunday calendar dates; reject non-Sunday dates. Audit/session instants use UTC. Derive Sundays using calendar arithmetic, not 24-hour UTC additions. Timezone changes apply only to newly opened dates. Existing dates retain their timezone snapshot, including when a draft is first created after opening the date, during draft refresh, and through amendments; never reinterpret an existing Sunday using a changed global timezone. A timezone-only global setting change MUST NOT stale or force-refresh an existing date/draft.

Roster: UUID identity, Unicode display name (1–100 characters after trimming), active status, restricted-qualified boolean. Reject control characters/newlines in display names. Duplicate names are allowed in the data model, but require a distinguishing public label, such as an initial, before both are active with identical normalized display labels. Normalization is trim+casefold; this is identity disambiguation, not an arbitrary roster limit. Names/labels are escaped in HTML/PDF. Deactivation is reversible and never deletes history. Do not hard-delete referenced volunteers.

Minimal CSV import: UTF-8 header `display_name,restricted_qualified`; boolean values `true`/`false`, imported people active. Preview errors and duplicates before committing; an all-or-nothing transaction creates new people only, never merges by name, never imports PIN plaintext, and never silently updates existing people. Credentials are provisioned separately. No synchronization system is included.

## 4. State, configuration and revisions

Each date has an availability-open flag, irreversible `first_published_at`, one current-publication pointer, at most one draft, and a monotonically increasing date-input epoch. Dates are unique and repeated open requests return the existing record.

Self-service writes require a valid link/session, active volunteer, open flag, and no first publication. A Scheduler may close and reopen collection before first publication; expiry or closure does not erase responses. It MUST remain closed after first publication, including during amendments. There is no invented calendar cutoff beyond link expiry, explicit closure, or first publication.

One availability row per volunteer/date contains two required booleans and a response revision. Both values update atomically. Absence means no response. Neither is an explicit submitted response. Staff edits use the same shape, record actor and confirmation reason, and after publication require an amendment draft. Availability updates never rewrite published payloads.

Settings have a monotonically increasing `settings_revision` for optimistic concurrency across **all** settings writes. Separately, `config_epoch` is the **draft-applicable schedule configuration epoch**: increment it when position essentiality, service labels, or optional service times change. A global timezone-only change increments `settings_revision` but does **not** advance `config_epoch` or stale an already opened date/draft, because its timezone is captured at date creation. Other unrelated security/link-setting changes likewise do not require rewriting existing draft snapshots. Roster identity/active/qualification changes increment a separate global `roster_epoch`. Availability and date lifecycle edits increment the date-input epoch. Draft mutations, locks, approvals, and configuration refresh increment the draft revision. Every increment and associated mutation/audit write is atomic.

A draft captures service labels/times, the **date's** immutable timezone snapshot, and all eight position essentiality settings. A new draft uses the current confirmed service labels/times/essentiality but ALWAYS takes timezone from `service_dates.timezone_snapshot`, including new amendment drafts. When a draft-applicable configuration value changes, existing drafts do not silently change: mark them stale and offer explicit configuration refresh with a diff. Refresh preserves slot identities and the date timezone, updates only draft-applicable configuration snapshots, invalidates affected approvals, increments revision, and requires revalidation. Publication is blocked on stale draft-applicable configuration (`config_epoch` mismatch). A timezone-only global change requires no draft refresh and does not block publication for an existing date. Live availability and roster eligibility always govern drafts; they are never frozen to permit stale eligibility.

For every generation/publication request capture `(draft_revision, date_input_epoch, roster_epoch, config_epoch)`. A request with stale expected values receives 409 and no mutation. Conservatively rejecting a request because an unrelated global roster change advanced an epoch is acceptable; losing that change is not.

When live inputs make an assignment invalid, retain its draft row and lock and flag the conflict. Do not silently remove it during an availability/roster update. Publication is blocked. The next explicit generation may replace invalid unlocked assignments; invalid locked assignments block generation until staff repair/unlock. Historical publications remain unchanged.

**Incremental repair of stale/invalid drafts:** A draft can already contain hard-rule conflicts caused by later availability, roster, or qualification changes; therefore manual repair MUST NOT require the entire draft to become valid in one edit. Validate the pre-edit and proposed post-edit conflict sets against *current* inputs. Permit an atomic repair-only edit (clear, reassign, swap, or unlock) when it strictly removes at least one existing conflict and introduces no new conflict or worsens an existing one; explicitly unlocking an invalid locked slot counts as removing its lock-specific conflict, even if its unavailable assignee still needs repair or regeneration. Unrelated/non-repair changes cannot be used to bypass validation. Leave remaining conflicts prominently visible, and apply the same authorization, revision checks, approval invalidation, and audit requirements. Each edit remains all-or-nothing. Only a fully valid draft, with permitted exact-pair approvals and required essential-vacancy approvals, can publish.

## 5. Scheduling constraints

### 5.1 Always enforced

- At most one non-null assignee per slot and at most one slot per person per service.
- Person must currently be active and have affirmative availability for that service. No response is never assignable.
- Positions 1/2 require current restricted qualification.
- A locked row is preserved exactly during generation, including an intentionally locked vacancy. Invalid or conflicting locks cause a conflict with no draft changes.
- Exactly 16 slot rows exist in every draft and publication. Vacancies are null assignees, never fake volunteer records.
- Editing multiple slots, including swaps, is all-or-nothing. Validate the final proposed state rather than an intermediate UI state. A normally valid draft must remain valid after each edit (except an atomically approved same-zone pair); an already invalid draft follows the strictly conflict-reducing repair-only rule in §4, without requiring all unrelated preexisting conflicts to be fixed at once.

### 5.2 Rotation and manual approval

A person assigned in both services must have different zones. Automatic generation never creates or retains a same-zone pair, even if it had a manual approval. A locked same-zone pair therefore blocks generation with the explanation that the strict solver cannot preserve it. Staff may keep it and finish manually, explicitly unlock/change it before generating, or cancel. No lock is silently dropped. An unlocked approved same-zone pair may be replaced by generation, invalidating its approval.

A manual edit that creates a same-zone pair requires a distinct approval confirmation with a nonblank reason (1–1000 characters after trimming). Submit the edit and approval together, tied to expected revisions, in one atomic operation. A cancelled or missing approval leaves the draft unchanged. This avoids persisting an unapproved proposed pair. The approval records volunteer, date, version, both slot identities/revisions/contents, approver and UTC time. It waives only zone rotation for that exact pair. New edits that retain an existing exact pair need no new approval; changes to either member do.

### 5.3 Essential-vacancy approval

Vacancies are legal in drafts. Every essential empty slot needs an explicit approval with reason before publication. **Approval is permitted even if an active, available, qualified volunteer could fill the slot**, including when a scheduler intentionally clears an assignment; no proof of staffing impossibility or solver-optimal shortage is required. Automatic generation still maximizes coverage under §6, and the review MUST show the vacancy plainly rather than imply no candidate exists. A single dialog may approve an enumerated list of slots with one reason, but persist a per-slot record. Optional vacancies require only a warning. Essentiality confirmation cannot be bypassed by approving all vacancies. The approving Scheduler/Administrator may be the publisher; the approval and publication are separately recorded.

Approval is never equivalent to filling a slot. Signal/PDF mark essential vacancies as approved and optional vacancies as optional. Show same-zone exceptions with a marker; approval reasons and actor details remain in authenticated detail/history, not distributed text/PDF.

### 5.4 Approval validity

A rotation approval is valid only for its exact unchanged pair in its draft version and while all other assignment rules are satisfied. A vacancy approval is valid only while its exact slot remains essential and empty. Slot content changes, clear/refill cycles, regeneration changes, and relevant classification/zone snapshot changes increment slot revision and permanently invalidate affected approvals. Eligibility changes affecting an approved pair invalidate that pair's approval. Reverting to the old contents does not restore an invalidated record. PIN reset, unrelated roster edits, and unrelated slot edits do not invalidate unchanged pair/slot approvals.

Approvals are not copied as valid into amendment drafts. New version, new approval. All invalidations/replacements remain in audit history. Re-evaluate exact validity at publication, never rely on a UI “approved” flag.

## 6. Solver implementation contract

Use Python OR-Tools CP-SAT behind an isolated service. No greedy replacement algorithm as the production generator. Only eligible `(person, service, position)` combinations have binary variables `x[v,s,p]`.

For each slot `sum_v x[v,s,p] <= 1`. For each person/service `a[v,s] = sum_p x[v,s,p] <= 1`. For each person/zone, `sum_(s,p in zone) x[v,s,p] <= 1`; this enforces zone switching when assigned twice. Locked assignee sets the corresponding variable to 1; locked vacancy sets all slot candidates to 0. Validate locks before solving.

Define binary `b[v]` for both services using `b<=a[v,1]`, `b<=a[v,2]`, `b>=a[v,1]+a[v,2]-1`.

Optimize lexicographically:

1. `E`: filled essential slots, maximize.
2. `O`: filled optional slots, maximize at fixed optimal E.
3. `B = sum_v b[v]`, maximize at fixed optimal E/O.
4. `U`: retain previous non-null assignees in the same unlocked slots, maximize at fixed optimal E/O/B. Previous vacancies are not stability rewards. Locks are already fixed.
5. **Sunday-specific canonical tie-break** at fixed optimal E/O/B/U: order slots by service then position. For a date, rank volunteers ascending by SHA-256 digest bytes of the UTF-8 string `church-scheduler-date-tiebreak-v1|YYYY-MM-DD|volunteer-uuid`, substituting that date's `service_dates.local_date` in ISO format and the lowercase hyphenated immutable volunteer UUID. Break theoretical digest collisions by UUID byte order; rank a vacant/null assignment after all volunteers. Choose the lexicographically smallest feasible assignment vector, using this date-specific volunteer ordering, by feasibility tests in slot order. This ordering is stable across runs, amendments, names, and configuration changes for the **same Sunday**, but may differ across Sundays. It deliberately uses no prior-service count, cross-date history, random runtime seed, or fairness/workload scoring. It does not guarantee weekly rotation among equally available volunteers. Never rely solely on rank sums to make ties unique.

Sequential solves MUST progress to the next objective only when the previous stage proves OPTIMAL. An early FEASIBLE result ends optimization and returns the current legal incumbent, explicitly unproven. A later-stage UNKNOWN retains the previous valid incumbent. Never claim the optimum of an unfinished stage. Cache the incumbent between stages. Validate all results independently of solver construction before commit.

Default total solve budget: 10 seconds per Sunday, covering objective stages and canonicalization, not 10 seconds per stage. Use one worker and fixed seed and canonical model ordering. Include deterministic-time counters in diagnostic metadata where supported. Wall-clock deadlines can interrupt at different points, so repeatability is guaranteed only when optimization and canonicalization complete on the pinned build; timeout incumbents may differ. Do not weaken the deadline to obtain repeatability.

Record raw status per stage, last completed stage, objective vector, bounds if available, canonicalization-complete flag, elapsed time, solver version and parameters, **date tie-break algorithm identifier**, input epochs, and sanitized input snapshot. Display:

- `OPTIMAL`: all four objectives proved and canonicalization completed.
- `FEASIBLE`: valid incumbent, with exactly which objectives remain unproved/canonicalization unfinished.
- `LOCK_CONFLICT`: prevalidation detected incompatible locks; original draft preserved.
- `UNKNOWN/TIMEOUT`: no usable incumbent; original draft preserved.
- `MODEL_INVALID` or unexpected `INFEASIBLE`: internal error, original draft preserved and sanitized error logged. Without conflicting locks, all vacancies is a legal solution; ordinary staff shortages are not infeasibility.

Do not commit a solver result if any captured epoch/revision changed. Return 409 and retain the newer draft. A nonoptimal draft remains publishable following ordinary validation and a visible “coverage not proven maximal” notice at final review. Never suggest that the priority guarantee was proved for a timed-out incumbent.

Diagnostics list actual vacancies, available/qualified counts, and relevant locks/rotation constraints. Do not assert that an empty particular slot has no eligible person unless that claim was established; a different equally good placement may fill it. With strict rotation, four restricted service-slots require four distinct qualified people; four qualified people alone is insufficient without compatible availability/locks.

## 7. User workflows

### 7.1 Volunteer

A date-scoped random shared link shows the date, service labels and searchable/paged active display labels. Select identity and enter personal PIN. After verification show only own current response and the four response choices. Save both service booleans atomically; success confirms date/choice. Reauthentication permits changes until first publication. Changing selection in the form does not save until submitted. A stale response revision returns 409 and asks the volunteer to review the latest choice, avoiding cross-tab lost updates.

Expired/revoked links deny access without exposing roster data. Closed dates show a contact-scheduler message. Publication permanently closes self-service. Existing verified sessions cannot bypass that guard. Do not expose other responses in API payloads, HTML hidden fields, counts, or browser caches.

### 7.2 Staff

1. Configure system, confirm essentiality, enter/import roster and distribute PINs privately.
2. Open a Sunday idempotently; copy its date link to Signal manually.
3. Dashboard: counts of first-only, second-only, both, neither, no response, plus service-available totals and qualified counts. The five response buckets partition the active roster exactly. New/reactivated active people without a response count as no response. Inactive responses stay in history but not active counts.
4. Generate initial draft or regenerate current draft. UI shows both service columns, position number, zone, essentiality, assignee/vacancy, lock, validation/approval markers. Do not rely on color alone.
5. Assign/clear/swap/lock/unlock; server validates the proposed result. If the draft was already invalidated by later inputs, allow §4 incremental conflict-reducing repair and show any remaining conflicts. A same-zone edit invokes explicit exception approval. Hard violations cannot be approved. Locking a vacant slot is visibly intentional and may reduce coverage.
6. Final publication review shows current inputs, all vacancies, exceptions, approval state and solver uncertainty. Approve specific essential vacancies with a reason, even if eligible staffing might exist. The same authorized Scheduler/Administrator may approve and publish, but publication requires a separate deliberate confirmation.
7. Copy plain text/download actual PDF for selected published version. Never make an unlabelled draft export available. MVP distributed exports are published versions only.
8. Amend from latest publication into a new draft. The currently published version stays current during editing. Record admin-confirmed availability changes in that draft's date workflow, repair assignments, obtain new approvals and publish a new numbered version. Earlier publications remain immutable/retrievable and visibly superseded. Copying an old version warns that a newer version exists.

Amend/create-draft retries return the existing draft for the date. Do not create competing drafts. Failed publication retains draft and current publication unchanged. No unpublish, rollback-to-open, automatic sending, or silent amendment is included.

## 8. Relational model and integrity

SQLite on local persistent storage, WAL, `foreign_keys=ON` on EVERY connection, bounded `busy_timeout` (default 5 seconds), `synchronous=FULL`. Migrations use Alembic with pre-migration backup. Application domain validation supplements, rather than replaces, SQL constraints. UUIDs are text in a consistent representation; booleans/check enums have CHECK constraints; timestamps UTC.

A compact schema may use service numbers directly instead of a redundant service-instance table. All fields below are required unless marked nullable. Standard creation/update timestamps and foreign-key indexes are implicit.

| Table | Fields and constraints |
|---|---|
| `admin_users` | UUID PK; normalized username UNIQUE; password hash; role CHECK admin/scheduler; active; credential epoch; encrypted TOTP secret nullable until enrollment; enrollment state; hashed one-time recovery codes in child rows. No deletion of audit actors; deactivate. |
| `volunteers` | UUID PK; display name; normalized active label; active; restricted-qualified boolean; PIN hash nullable; credential epoch; row revision. Enforce unique normalized active labels with partial unique index. |
| `settings` | singleton ID CHECK 1; timezone; two labels/times; essentiality-confirmed flag; `settings_revision` for all settings writes; `config_epoch` only for draft-applicable settings/position essentiality; `roster_epoch`; security/link settings. |
| `positions` | number PK CHECK 1..8; fixed zone/qualification seeded as §3; essential boolean; change through restricted configuration service. |
| `service_dates` | UUID PK; local_date UNIQUE; timezone snapshot; availability_open; first_published_at nullable; date_input_epoch; current_published_version_id nullable. Cross-date pointer prohibited by composite FK to `(date_id,id)` of schedule versions. |
| `date_links` | UUID PK; date_id FK; token digest UNIQUE; generation; created/expiry/revoked timestamps; creator. One unrevoked current link per date via partial unique index. |
| `availability` | `(date_id,volunteer_id)` composite PK/FKs; service_1_available, service_2_available booleans; revision; source CHECK volunteer/admin; actor reference; confirmation reason nullable for volunteer. |
| `schedule_versions` | UUID PK; date_id FK; draft/published status; publication_number nullable for draft; draft revision; base_version_id nullable; config snapshot and epoch; creator; published actor/time nullable; input epochs and solver metadata nullable; canonical published payload/hash nullable until published. UNIQUE(date_id,id); UNIQUE(date_id,publication_number); partial UNIQUE(date_id) WHERE status='draft'. Base-version composite FK restricts it to same date. |
| `slots` | `(version_id,service_number,position_number)` composite PK; date_id; volunteer_id nullable FK; slot_revision; locked; source manual/solver; zone and essential snapshots. service CHECK 1/2; position FK; composite FK `(date_id,version_id)` to versions. Partial UNIQUE(version_id,service_number,volunteer_id) WHERE volunteer_id IS NOT NULL. UNIQUE(date_id,version_id,service_number,position_number) supplies the parent key for date-scoped approval FKs. Sixteen rows created with each draft in one transaction. |
| `rotation_approvals` | UUID PK; version_id/date_id/volunteer_id; first and second slot position numbers; both slot revisions/fingerprints; reason; approving user/time; invalidated_at nullable. Composite FKs to both slots in that same version and date; service 1/2 identities fixed. Active approval for a pair UNIQUE via partial index. |
| `vacancy_approvals` | UUID PK; same-version composite slot FK; slot_revision/fingerprint; reason; approving user/time; invalidated_at nullable. One active approval per slot via partial unique index. |
| `web_sessions` | hashed opaque token PK; staff/volunteer kind; actor ID; volunteer date/link ID where applicable; credential epoch; created/last-used/expiry/revoked times; CSRF state. Separate explicit FKs or separate staff/volunteer tables, never unverifiable generic actor IDs. |
| `auth_throttles` | keyed by hashed scope identifier; window/counters/blocked-until; expiry for cleanup. Persist across restarts; atomic counters. Never store guessed credentials. |
| `idempotency` | actor_id/date_id/operation/key UNIQUE; request digest; resulting version ID; created time. Insert with the publication transaction. |
| `audit_events` | UUID PK; UTC time; action; object identity; staff FK nullable; volunteer FK nullable; actor type system/staff/volunteer with CHECK for applicable identity; sanitized before/after details and input/revision metadata. Append-only to application users. |

Do not store duplicate PIN plaintext or raw session/link tokens. Store credentials separately from public roster serializers. Restrict deleting referenced rows (`ON DELETE RESTRICT`); deactivate/revoke instead. No user-facing hard-delete API or automated historical-data purge in MVP. Retention of operational data follows deployment policy through documented maintenance; daily backup retention defaults to 30 copies.

SQL CHECKs cannot count all 16 rows or enforce all cross-row rules. The transaction service MUST check the exact slot set `(1..2)×(1..8)`, date ownership, snapshots, approval pair ownership and all eligibility at publication. Integration tests must reject malformed direct SQL references using the actual migrations.

Freeze published version content, slots, and approval children using database triggers preventing INSERT/UPDATE/DELETE against published parents (including attaching new children). Permit the one draft→published transition only after service validation. Published status never changes to superseded; derive that label from the date's current pointer. No normal application path rewrites a published payload. DB triggers are accidental-mutation protection, not protection against a host administrator.

Published canonical JSON payload is the authoritative source for historical view/text/PDF: date/timezone, service labels/times, version number, all positions/zones/essential flags, volunteer UUID and displayed name per occupied slot, vacancies, applicable approvals/markers, approving identities/reasons for authenticated history, publisher/time and input provenance. Later renames/deactivation/configuration changes cannot change it. Store a SHA-256 content hash for consistency, not as a digital signature. Source rows and payload must match at publication.

Index availability by date and volunteer; slots by version; versions by date/publication; audit by object/time; active roster by normalized label; sessions/throttles by expiry. Use bounded pages/search and eager/batched joins. Do not issue one query per person or slot. No index or schema assumes 20 or 100 is a maximum.

## 9. Transaction and race contract

Use short `BEGIN IMMEDIATE` write transactions, with bounded retry only for database-busy failures before a mutation commits. Roll back whole units on failure. Persistent busy timeout gives 503/retry guidance, never an unhandled database error or blind overwrite.

- Availability write: verify session scope/epoch and link/date state inside the transaction, compare response revision, write both booleans, increment date epoch, invalidate affected approvals if needed, append audit, commit.
- Roster/configuration write: compare row/`settings_revision`, mutate, advance only the corresponding input epoch(s), flag only affected drafts/invalidate affected approvals, audit, commit. In particular, a timezone-only global change advances the settings revision but not `config_epoch`, does not change `service_dates.timezone_snapshot`, and does not stale existing drafts.
- Draft edit/swap/repair: compare expected draft/input revisions; validate the complete proposed state if the draft was valid, otherwise apply §4 strict conflict-reduction for repair-only edits. Update touched slots and revisions, invalidate affected approvals, insert any explicitly approved new pair, increment draft revision/audit, commit. For swaps, temporarily clear only touched assignees inside the transaction before assigning final values, so immediate UNIQUE checks do not reject a valid swap. No intermediate state is visible. An invalid preexisting assignment may be cleared without requiring unrelated invalid slots to be repaired in the same transaction.
- Generation: read one consistent input snapshot, end read transaction, solve outside SQLite, validate result, acquire write transaction, compare all captured epochs/revisions and draft identity, replace changed unlocked slots, update revisions/approvals/solver metadata/audit, commit. Locks unchanged. Failure leaves original draft intact. No long transaction around CPU work.
- Publication: authorize; check idempotency record first; compare request digest for retries; acquire write transaction; recheck all revisions, first-publication state, current pointer/base version, setup/config freshness, exact slot count, current active/available/qualified state, exclusivity, locks and approval fingerprints; allocate `max(publication_number)+1` within this serialized transaction; snapshot canonical payload; mark version published; atomically set current pointer and first_published_at if absent; permanently close availability; revoke volunteer date sessions; append audit and idempotency record; commit.

A publish retry with identical actor/key/digest returns the originally published result even if the draft is now published. Reusing key with different content gets 409. Two different publish requests for the same draft must yield one publication and a conflict/replay response, not two versions. Concurrent amendment creation yields one draft. Current published pointer and draft base prevent publishing an amendment over an unexpectedly newer publication.

Race outcomes are defined by commit order. If availability changes before publication, publication revalidates it or returns conflict. If publication commits first, the volunteer write fails closed. A deactivation/qualification edit before publication blocks stale assignment publication; one after publication leaves historical content unchanged and flags any active draft. Transactional audit failure rolls back the business mutation. Connection loss after commit is recoverable through idempotent retry or refresh.

## 10. Security contract

### 10.1 Shared link and volunteer PIN

- Generate date-link token with 32 random bytes from OS CSPRNG, URL-safe encoded. Store only SHA-256 digest, plus expiry and revocation. Default lifespan 30 days from issuance, configurable positive duration. Each date has one current link. Rotation generates a new token, revokes the old link and its sessions atomically; availability responses persist. First publication disables all writes independently of expiry.
- A token cannot be recovered from its digest. Return it once on create/rotation for copying; when lost, rotate. No GET that retrieves plaintext later. UI explicitly explains replacing the Signal link.
- Default PIN: CSPRNG eight decimal digits, retain leading zeros; configuration may choose 6–12 digits, minimum six. No sequential/default/reused shared PIN generation. Show new/reset PIN once to Administrator over HTTPS, distribute privately. Never send it through Signal group, URL, CSV export, audit, logs, or analytics.
- Hash PINs and staff passwords with salted Argon2id, starting at memory 19 MiB, iterations 2, parallelism 1, tune upward subject to benchmark. Rehash outdated parameters after successful verification. Short numeric PINs remain limited against offline guessing; protect backups and offer longer generated PINs. This MVP does not claim PINs are MFA.
- Verification is server-side. Generic wrong identity/PIN response. Dummy verification for unknown IDs where appropriate, while applying throttles first. No response data before successful verification.
- Initial configurable throttles: five failed attempts per volunteer across dates within 15 minutes, then block verification for that volunteer for 15 minutes; 30 verification attempts/IP per five minutes; global 120 verification attempts/minute. Also rate-limit valid-link roster GETs at 60/IP/minute. Admin login: five failures/account per 15 minutes plus 30 attempts/IP per five minutes. Counts/blocks are atomic and persist; blocked requests return 429 with Retry-After without hashing. Cleanup expiry, no permanent lockout, Administrator can clear blocks with audit. PIN reset clears that person's block. Shared NAT and deliberate temporary lockout risk must be tested/documented; defaults are tunable security settings, not volunteer scheduling rules.
- Bound concurrent credential hash operations to two on the baseline VM; reject or briefly bound queue rather than exhaust RAM. Trust client IP only from explicitly configured reverse proxy addresses.
- Verified volunteer session: random opaque server-side token, 30-minute idle/2-hour absolute expiry, scoped to volunteer+date+link generation+credential epoch. Derive identity from session; ignore/reject client-supplied alternate identity on mine endpoints. Reset PIN/deactivate/rotate link/first-publish revokes relevant sessions immediately.

### 10.2 Staff authentication

Individual username/password with TOTP required for both roles. Password minimum 15 characters, maximum at least 128, allow paste/password managers, no arbitrary character composition or scheduled expiry. Apply staff throttles to password and TOTP attempts; do not issue full staff session before completing MFA. TOTP default 30-second steps, at most ±1 step clock skew; prevent successful reuse of the same step. Synchronize host time.

Bootstrap uses a one-time local management command to create the initial Administrator, with no reusable default password or open web registration. Enrollment is required before privileged access. Encrypt TOTP seeds with a dedicated key outside database and source control. Give one-time recovery codes, store hashes only, consuming each atomically. Local host-access recovery can reset MFA/credentials with audit and session revocation; no emailed reset system. Prevent deactivating/demoting the last active Administrator.

Staff sessions: 30-minute idle/8-hour absolute expiry; rotate identifiers at login/MFA completion, revoke on logout, password/MFA reset, role changes or account deactivation. Check current role/credential epoch on each request. Bootstrap and recovery never appear as unauthenticated public API endpoints.

### 10.3 HTTP, browser and output controls

HTTPS at Caddy/existing proxy; internal app only, no public app/DB port. Use separate staff/volunteer Secure, HttpOnly, SameSite=Lax host cookies. CSRF tokens and Origin checks for every cookie-authenticated mutation, including PIN verification, login, link rotation and publication. No mutation on GET. CORS same-origin only; no wildcard credentialed CORS.

No third-party scripts/assets on token pages. `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, CSP with same-origin assets and no framing, `X-Content-Type-Options: nosniff`. Add HSTS when HTTPS is fully operational. Exclude token-bearing URLs and request bodies from proxy/app logs; redact cookies, authorization, passwords, PINs, TOTP/recovery data and secrets. Signal preview crawlers receive no availability/schedule data and cannot mutate anything.

Use parameterized SQL and template escaping. Render PDFs from fixed templates/data only, with network/file access disabled for untrusted content; never accept arbitrary HTML/remote image URLs for rendering. Bound text lengths, body size (default 1 MiB, configurable for roster CSV), page sizes and generation requests. Allow arbitrarily growing rosters through pagination/bounded batches rather than arbitrary person limits. Error responses contain no stack traces or SQL. Detailed logs are restricted to operators.

Audit successful staff mutations, volunteer response changes, credential lifecycle, approval invalidation and publication with actors and revisions. Authentication failures may be aggregated/rate-limited in security logs to avoid log flooding. Never log secrets or raw shared-link tokens. Do not pretend append-only application audit is tamper-proof against host/database owners.

## 11. API semantics

Routes are indicative; controls/state semantics are mandatory. Use HTML/HTMX pages over the same application services as API handlers.

| Surface | Required behavior |
|---|---|
| GET `/availability/{token}` | Validate token, show minimal roster/date or closed/expired state, no private response. |
| POST `/availability/{token}/verify-pin` | CSRF, throttle, verify selected immutable ID/PIN, issue scoped session. |
| GET/PUT `/availability/{token}/mine` | Scope checks every request; return/write only own booleans and response revision; writes enforce publication guard. |
| Staff login/logout/MFA/recovery | §10; local bootstrap/recovery separate. |
| Roster/import/PIN/settings APIs | Operation matrix, row revisions, audit, input epoch updates. |
| Date open/close/link rotate/summary | Idempotent date identity; no plaintext token recovery; summaries staff-only. |
| Generate/edit/multi-slot swap/lock/config refresh | Expected draft/input revisions, atomic final-state validation or §4 repair-only strict conflict reduction when the preexisting draft is already invalid; explicit draft-applicable config refresh never replaces date timezone. |
| Approval endpoints | Exact version/slot revisions, nonblank reason; atomic edit+pair approval supported. |
| Publish/amend/history | Expected revisions, publish idempotency key, immutable versions, one draft. |
| Signal text/PDF | Staff authorization; explicit published version; payload-only historical rendering. |

Response codes: 401 unauthenticated, 403 unauthorized, 404 for invalid/inaccessible public token/object where useful to avoid disclosures, 409 stale revision/state/idempotency conflict, 422 invalid fields or forbidden scheduling proposal, 429 throttle/busy solver, 503 database/worker temporarily unavailable. Return actionable sanitized messages and current revision metadata only to authorized actors. No endpoint accepts role, approver identity, publication time, or source actor as trusted client assertions.

## 12. Deployment and operations

One Debian VM on Proxmox; Docker Compose with application and Caddy or existing TLS proxy; local SQLite persistent volume; no network filesystem for live database. Python 3 supported release at implementation, FastAPI, SQLAlchemy/Alembic, Jinja templates and HTMX, OR-Tools CP-SAT. Use a maintained fixed-template PDF library such as ReportLab; preserve names through a bundled Unicode-capable font and wrap long labels. Pin a tested dependency lockfile and image digests and document the exact supported versions. Resolve versions during implementation rather than hardcoding an unverified current release here.

One application worker and a bounded local solver subprocess (one generation at a time) suffice. CPU solving must not run on the async event loop or block volunteer requests. Hold an app-wide generation semaphore; concurrent requests get 429/retry guidance. A crashed/killed solver returns an explicit error without commit; release semaphore in cleanup. No durable background queue or Redis is needed for a synchronous bounded operation. Rate-limit/bound PDF work as well. Keep request timeouts above the 10-second solver deadline plus transaction/response overhead (initial proxy timeout 30 seconds).

Baseline measurement environment: dedicated 2 vCPU, 2 GiB RAM, local SSD VM, declared CPU model/container limits and dependency versions. This is a measurement target, not an untested performance guarantee. Run containers non-root with minimal writable paths/capabilities; secrets mounted outside repository, persistent keys protected and backed up. Expose minimal no-detail liveness/readiness, detailed health to staff only; readiness checks database and schema. Fail startup rather than serve with mismatched migrations/missing encryption key.

Daily consistent backup via Python SQLite backup API or equivalent supported API, not a raw copy of an active `.db` ignoring WAL. Default retain 30 daily copies, adjustable. Include deployment configuration, schema version, recovery instructions and required encryption keys in separately protected recovery material. Restrict permissions, encrypt off-host backups, expose last successful backup/failure in staff health. Backups are maintenance CLI/systemd/host-scheduler jobs, not new scheduling features. No off-host provider dependency is required.

Restore into a clean environment: check SQLite integrity and foreign keys, migration compatibility, recreate exact latest payload/text/PDF semantics, authenticate staff including MFA/recovery, then invalidate restored sessions and rotate public links before public reopening. Record measured recovery procedure. Rehearse before release and periodically afterward. Daily backup implies potential loss since the last successful backup; no zero-loss claim. Monitor disk space; handle full disk with rollback and failure response. Use a temporary file and atomic rename for generated backup/PDF files where persisted.

## 13. Acceptance criteria

| ID | Required result |
|---|---|
| AC-01 Setup | All eight essential flags explicitly confirmed before any publication; service labels/timezone required, times optional. |
| AC-02 Roster | Add/edit/import/deactivate/qualify without a roster ceiling; credential rights enforced; published names preserved. |
| AC-03 Verification | Correct PIN grants only the selected person's date-scoped access; throttles/expiry/reset revocation work; no plaintext stored/logged. |
| AC-04 Availability | First-only/second-only/both/neither/no-response distinct; paired atomic submission, correct active dashboard counts. |
| AC-05 Deadline | Self-service changes work before first publication and fail afterward, including races/existing sessions/amendments. |
| AC-06 Staffing | Unique per slot/service; only active, explicitly available and qualified people; exactly 16 rows. |
| AC-07 Rotation | Solver always alternates zones; manual same-zone edit is atomic with exact-pair approval; no hard-rule waiver. |
| AC-08 Partial | Legal partial and all-empty drafts, honest diagnostics, no silent constraint relaxation. |
| AC-09 Priorities | Proven solutions match lexicographic E/O/B/U and Sunday-specific SHA-256 canonical tie-break; repeated completed solves for the same Sunday/input produce the same result; unproven incumbents clearly distinguished. |
| AC-10 Editing | Atomic edits/swaps, locks including vacancies preserved, invalid locks block generation, stale edits conflict; administrators can resolve two or more preexisting hard conflicts incrementally without temporarily passing publication validation. |
| AC-11 Publication | Specific essential vacancies explicitly approved even if eligible volunteers remain; the same Scheduler may approve and publish with separate audit events; optional vacancies warn; stale approvals/inputs/hard conflicts block. |
| AC-12 Versions | One draft/current pointer, immutable payload/children, numbered amendment publications, new approvals, old views unchanged; existing Sunday timezone is never changed by a global setting update or draft refresh. |
| AC-13 Outputs | Actual downloadable PDF and Signal text match selected immutable payload; date/labels/names/positions/vacancies/markers/version present, private reasons omitted. |
| AC-14 Concurrency | Draft/input/response compare-and-swap and publish/amend idempotency prevent lost updates/duplicate versions. |
| AC-15 Operations | TLS/MFA/role checks, restart persistence, consistent backup and clean restore including keys, sensitive-log review passed. |
| AC-16 Usability | Nontechnical staff completes open→share→generate→approve→publish→copy without shell; volunteer flow works at phone width with labels/keyboard support. Initial deployment/bootstrap/recovery may require operator shell. |
| AC-17 Scale | §15 workload passes with at least 100 volunteers and no schema/infrastructure change or fixed roster assumption. |
| AC-18 Integrity/security | §14 additional tests pass on migrated SQLite and actual role/session routes, not only mocked repositories. |

## 14. Automated edge-case and regression plan

Retain v1.0 T01–40 with these explicit fixtures/results. Every service test asserts resulting assignments plus audit, revision, approval, transaction and version side effects where applicable.

| ID | Fixture/action | Expected |
|---|---|---|
| T01 | Eight both-service volunteers, four qualified, compatible free placement | Fill 16, all eight alternate zones. |
| T02 | Seven both-service volunteers | At most 14 filled, vacancies remain, no duplicates. |
| T03 | Four distinct qualified with compatible availability and other staff | Four restricted slots filled; count alone not asserted sufficient. |
| T04 | Three qualified, all positions essential | At most three automatic restricted fills, honest vacancy. |
| T05 | Qualified, available person proposed outside twice | Requires atomic reasoned approval, then valid manual pair. |
| T06 | Unqualified proposed in position 1 with approval | Reject and no mutation. |
| T07 | Qualified people first-only | No second-service restricted fills from them. |
| T08 | First-only response | Never assign second. |
| T09 | Neither response | Not assigned, bucket neither. |
| T10 | No response | Not assigned, bucket no response. |
| T11 | Two slots same person/service | Reject final-state edit atomically. |
| T12 | Outside then inside | Valid without approval. |
| T13 | Inside then inside, no reason/approval | Manual edit rejected; solver cannot produce pair. |
| T14 | Change approved pair member/person/slot | Permanently invalidate approval, require new. |
| T15 | Scarcity with essential/optional tradeoff | Proved E dominates O. |
| T16 | Equal coverage, different double-service count | Proved B maximal; no coverage reduction. |
| T17 | Essential vacancy unapproved | Publish fails and lists slot. |
| T18 | Specific essential vacancy approved | Publish succeeds only if all else valid. |
| T19 | Optional vacancy | Warn, publish without waiver. |
| T20 | Approved vacant slot filled then emptied | Old approval never resurrects. |
| T21 | Valid assigned/vacant locks | Preserved by generation. |
| T22 | Incompatible locks | No draft change, actionable conflict. |
| T23 | Withdraw availability after generation | Draft retains visible invalid assignment; one slot can be repaired while another remains invalid, publication blocked until all repaired; unlocked regeneration can repair. |
| T24 | Amend availability before publication | Latest committed response or 409, retained audit. |
| T25 | Volunteer writes after first publication | Denied, including during amendment. |
| T26 | Unauthenticated staff routes | No private data or mutation. |
| T27 | Wrong PIN/rapid guesses/expired link | Generic failure, bounded hashing, 429/denial. |
| T28 | Deactivate after publication | History unchanged, excluded from future eligible pool. |
| T29 | Change essentiality | History unchanged, draft refresh required, affected approvals invalid. |
| T30 | Two edits same revision | Exactly one commit, other 409. |
| T31 | Duplicate publication | One result/version; same-key replay returns it. |
| T32 | Amendment publication | New version, prior immutable, new approval review. |
| T33 | Timeout with valid incumbent | FEASIBLE with proof-stage metadata; no false optimum. |
| T34 | Solver/locks incompatible | Original locks/draft intact. |
| T35 | No responses | Legal 16-vacancy draft, all essential approvals required. |
| T36 | Qualified candidates constrained by other assignments | Conservative diagnostic, no false “none qualified.” |
| T37 | DST boundary Sunday | Correct local date/grouping, UTC audit. |
| T38 | Restart app/proxy | Durable roster, responses, approvals, drafts/publications; valid session policy applied. |
| T39 | Clean restore | Integrity checks, staff MFA access, same outputs, sessions invalidated and links rotated. |
| T40 | Reset PIN | Old PIN/session fail, new PIN works, secret-free audit. |

Additional required tests:

| ID | Scenario and assertion |
|---|---|
| X01 | Small exhaustive oracle independent of solver code enumerates legal assignments for reduced candidate/slot fixtures and compares E/O/B/U/canonical vector. Include cases where greedy coverage/stability fails. Injected stage statuses prove FEASIBLE stops lower priorities and later UNKNOWN retains incumbent. |
| X02 | Property/generated tests on full eight-position rules across randomized rosters, responses, essential flags and compatible locks; independent validator checks hard rules and exact slots. Multiple optimum fixtures prove canonical output when completed. |
| X03 | A→B→A pair edit, fill→clear vacancy, unrelated edits, and exact no-op edit: invalid approvals never revive, unaffected approvals survive, genuine no-op does not increment slot revision. |
| X04 | Locked approved same-zone pair blocks strict regeneration without discarding locks; manual publication with valid approval remains possible. |
| X05 | Availability→publish, publish→availability, roster/qualification→publish, config→publish races using two actual SQLite connections and controlled barriers; assert commit-order contract. |
| X06 | Slow solver plus availability/roster/config/draft/approval change: no stale commit. Worker crash, UNKNOWN without incumbent, invalid model, busy database and full-disk fault: no partial changes. |
| X07 | Concurrent open/amend/publish, different/same idempotency keys, same key different payload, retry after lost response: uniqueness/current pointer maintained. |
| X08 | Actual migration FK/UNIQUE/CHECK tests reject cross-date slot/base/current pointer/approval references, duplicate assignee/draft/publication number, illegal services and published child mutations. Test foreign keys on every pool connection. |
| X09 | Volunteer ID/date/version substitution and expired/revoked/session-epoch mismatch: cannot read/write others or export/history/admin. Scheduler cannot reset PIN/change roles/settings. Missing/forged CSRF and hostile Origin reject all cookie mutations. |
| X10 | PIN/login throttles across dates/IPs, spoofed forwarded IP, concurrent guesses, restart persistence, shared NAT, temporary block expiry; TOTP replay and recovery-code double use prevented. Last-admin protection and local recovery verified. |
| X11 | Names/reasons with markup, long Unicode/RTL labels and formula-like CSV strings are safely rendered/treated as text; PDF makes no external network/file fetches; downloads paginate without clipping, contain no private reasons/PINs. |
| X12 | Rename/deactivate/config changes after publication: payload/hash and historical text remain identical; PDF content remains identical semantically (renderer metadata timestamps need not be byte-identical). Superseded status correct. |
| X13 | Concurrent two-tab response revision updates, no-response vs neither, new/inactive/reactivated volunteers, duplicate labels, leading-zero PIN, atomic CSV failure, token rotation with existing sessions. |
| X14 | Token/credential log and cache checks at proxy and app, no third-party asset requests, GET/link preview never mutates, error responses sanitized. |
| X15 | Browser workflows on phone/desktop: volunteer update, admin generate/edit/approve/publish/amend/copy/PDF; stale-state conflict recovery, no color-only indicators. |
| X16 | Scale workload §15, bounded query counts, generation remains responsive to public availability traffic. |
| X17 | Two distinct Sunday dates with deliberate equal-E/O/B/U solution fixtures: compute date-specific SHA-256 volunteer ranking independently and verify exact lexicographically canonical assignment for each; select fixtures demonstrating ranking can differ by date. Repeated complete runs for one date/input match; rename does not change rank. No assertion that assignments MUST differ every Sunday or that fairness is optimized. |
| X18 | Two volunteers withdraw or lose qualifications after a draft exists: clearing one invalid slot persists while the other conflict remains visible; unlocking an invalid locked slot enables later generation/repair; an unrelated edit or any edit introducing/worsening a hard conflict is rejected; full repair then publish works. Assert atomicity, per-slot revisions, approvals and audit history. |
| X19 | Intentionally clear an essential slot even though another qualified/available person could fill it; the same Scheduler approves it with reason and separately publishes. The vacancy remains marked as approved in immutable output and audit. Without approval, publication is blocked. |
| X20 | Change only global timezone after opening a date: no stale draft, `config_epoch` unchanged, date timezone and amendment/refresh unchanged, subsequent new dates use the new timezone. Service label/time/essentiality change advances `config_epoch`, stales drafts, requires explicit refresh, and never overwrites date timezone. Verify `settings_revision` conflict on concurrent settings edits. |

Use pytest for domain/integration and Playwright or equivalent for essential browser journeys. Use real migrated on-disk SQLite in concurrency/integrity/restore tests, not solely in-memory/mocked repositories. Fault injection is acceptable for disk/worker/network failure. Property seeds and scale fixtures are reproducible; do not use real volunteer data. A passing unit validator alone is insufficient proof of scheduling objective correctness or transaction isolation.

## 15. Growth and performance acceptance

No arbitrary total-volunteer limit, no hardcoded initial qualified count. UI search/pagination defaults 50 per page (maximum request page 100), with further pages; these are bounded-response sizes, not roster caps. Batch roster/availability queries; never fetch every historical response to render the current date. Availability identity lookup remains usable at 100 people.

Required benchmark dataset: at least 100 active volunteers, a documented mix of none/first/second/both responses, restricted qualification counts including more than four in a separate fixture, eight positions, two services, and every Sunday of a five-Sunday calendar month. Apply the Sunday-specific canonical tie-break without introducing historical fairness state. Include full coverage, restricted shortage, mixed essential/optional, locked, and partial scenarios. Open/collect/generate/publish/amend per date using the same single-Sunday APIs. No cross-date allocation or monthly batch UI is added.

On the §12 baseline with warmed app/database, record at least 20 generation runs spanning these fixtures and 100 mixed ordinary HTTP operations, including 20 concurrent volunteer clients and two staff clients:

- Each solver invocation terminates within the 10-second configured budget plus at most two seconds of orchestration allowance. At least 95% of normal benchmark fixtures complete proven E/O/B/U within 10 seconds; report canonicalization completion separately. Shortage fixtures also satisfy hard rules. Any benchmark timeout is visible, legal if an incumbent exists, and investigated before claiming performance acceptance.
- Ordinary page/read/write p95 <=1 second excluding network/TLS setup on the local test network, including during one active generation. PIN verification p95 <=2 seconds under the stated bounded load; expected throttle responses reported separately, never hidden as successful availability saves.
- No lost availability updates, integrity errors, unexpected 5xx, OOM, or indefinitely waiting requests. Combined application+solver+proxy RSS remains below 1.5 GiB on the baseline, excluding OS disk cache. Record host/container peaks.
- Roster/current-date summary queries use a bounded number of SQL queries independent of people displayed (target <=10 SELECTs excluding auth and audit writes). Show query counts and plans for date lookups at 20 versus 100 volunteers. No per-person lazy query growth.

If a target fails, optimize bounded local work/indexes/model before proposing new infrastructure. Document measured results, hardware, solver stage outcomes and limits; do not fabricate performance evidence. Correct hard constraints always take precedence over timing.

## 16. Delivery and release

Deliver repository/source; pinned lockfiles/images; migrations; reproducible fixtures; all AC/T/X results; Compose and environment example without secrets; operator setup/upgrade guide; staff/volunteer PIN and recovery instructions; security settings defaults; backup/restore scripts and rehearsal evidence; benchmark results; known limitations.

Implementation order: foundation/auth/SQLite migrations and integrity tests; roster/setup/availability with concurrency; solver and oracle; editing/approvals; atomic publication/snapshots/exports/amendments; browser/security/scale/restore release checks. Do not defer concurrency and authorization to post-release hardening.

Required live configuration: actual essential list, church timezone/service labels, optional actual times, roster and qualifications, privately distributed PINs, staff MFA, DNS/TLS/proxy trust, secrets and backup destination. These do not block development. Publication is blocked until setup is complete; production readiness requires passing the release tests, not just compiling or rendering the happy path.

## 17. Primary implementation references

- https://developers.google.com/optimization/cp/cp_solver
- https://sqlite.org/isolation.html
- https://sqlite.org/foreignkeys.html
- https://sqlite.org/backup.html
- https://fastapi.tiangolo.com/async/
- https://fastapi.tiangolo.com/deployment/https/
- https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html
- https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html

## 18. v1.2 decision record

**Business decisions, confirmed October 23, 2026:**

1. When scheduling objectives E/O/B/U are tied, use deterministic volunteer ranking derived from each Sunday's local date and immutable UUID (§6), not the same fixed UUID rank for every date. No cross-week fairness scoring or guaranteed volunteer rotation is added.
2. An authorized Scheduler/Administrator may approve publication of a specifically identified essential vacancy even when an eligible volunteer could be assigned, with a reason and visible vacancy marker (§5.3). This is a manual publication decision, not a relaxation of generator optimization.
3. An authorized Scheduler/Administrator may approve an exception or essential vacancy and publish the same schedule without a second approver; approval and publication remain separately recorded (§2, §5.3).

**Implementation clarifications, required:**

4. Stale drafts with multiple existing invalid assignments support conflict-reducing one-edit-at-a-time repairs, including unlocking invalid locks; strict final publication validation remains mandatory (§4, §5.1, §9).
5. A global timezone setting change affects newly opened dates, not an existing Sunday's immutable timezone snapshot. `settings_revision` detects concurrent setting changes; `config_epoch` tracks only draft-applicable changes so timezone-only changes do not make old drafts stale (§3–4, §8–9).

The confirmed decisions and clarifications above are normative in their referenced sections. They do not add monthly scheduling, workload fairness, public schedules, second-person approvals, or any new infrastructure requirement.
