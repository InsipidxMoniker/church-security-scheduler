# Development roadmap

Current application version: **v1.0.4**. The milestones below are proposals, not commitments, deadlines or authorization to implement changes. Released outcomes are in the [changelog](CHANGELOG.md); current operations and outstanding production acceptance checks are in the [README](README.md).

Any future work must preserve eligibility, per-service uniqueness, automatic zone rotation, explicit manual exceptions and essential-vacancy approvals, publication history, concurrency controls, staff authorization and MFA. Existing responses and publications must not be reinterpreted. Schema changes need a forward migration and a recovery plan that preserves the original encryption key and existing PBS backup arrangements.

The [v1.2 MVP specification](docs/church_security_scheduler_mvp_spec_v1.2.md) is the v1.0.0 baseline, not the application version. The [v1.0.3 amendment](docs/v1.0.3-qualification-zones.md) records approved later changes. New policy changes require explicit decisions and independent tests.

## v1.1.0 — Service-specific preferences and usability

Propose optional inside/outside preferences for each service of a Sunday, collected during PIN-verified availability and, when confirmed by the volunteer, recorded by staff. Preferences would be advisory and never override availability, qualifications, locks, zone rotation, coverage or publication approvals. Existing responses would default to no preference; historical publications would remain unchanged.

Decide before implementation:

1. Capture per-Sunday preferences, persistent defaults, or both; the proposed starting point is per-Sunday and per-service only. Decide whether changing availability clears or retains a preference.
2. Set preference precedence in the **current** `E > O > RS > RT > RP1 > B > U > canonical tie-break` sequence. Placing it before `U` can reduce assignment stability; placing it after `U` preserves more existing assignments. Obtain explicit approval for the ordering.
3. Choose per-assigned-service or two-service-pattern scoring; the proposed starting point is equal weight per assigned preferred service.
4. Define draft staleness, concurrency and post-publication amendment behavior when preferences change.
5. Decide what staff see for unmet preferences and whether preferences appear in exports; the proposed default is staff-only display.

Update solver snapshots, independent validation, diagnostics and oracle tests together, and measure the shared ten-second solve budget. Existing locked same-zone pairs continue to block strict regeneration, even if manually approved, unless that behavior is separately changed.

Also propose progressive improvements to navigation, draft and vacancy presentation, responsive layouts, accessibility and discoverability of common edits. Preserve server-side authorization and the existing server-rendered workflows unless a broader change is justified. Release acceptance requires compatible migration and response behavior, preserved scheduling and publication safeguards, and mobile/browser regression coverage.

## v1.2.0 — Monthly planning and assignment history

Consider a calendar or consolidated view of Sundays, availability, drafts, approvals and publications, plus per-volunteer assignment and service/zone history. Bulk review need not imply bulk creation or publication.

Decide whether multi-Sunday generation belongs here, how date-specific locks and approvals interact, and whether reports include drafts. For history, identify volunteers by stable ID and choose which publication version counts; amendments must not be counted as separate completed Sundays. Preserve independent single-Sunday operations and immutable history.

## v1.3.0 — Fairness-aware scheduling

Consider workload reporting and an explicitly approved historical fairness objective with reproducible tie handling. Decide the lookback period, counting basis, treatment of no-shows and unavailable volunteers, opt-outs, and precedence relative to coverage, preferences and assignment retention. The application does not currently track attendance, completion or acceptance; those counting bases require additional data and decisions. Validate competing objectives with an independent solver oracle and do not silently rescore history.

## v1.4.0 — Communications and reminders

Evaluate optional schedule delivery, availability reminders and publication notifications, with previews, delivery state, bounded retries and duplicate prevention. Decide the supported channel and integration, recipient identity and consent, credentials, delivery persistence, and behavior for amendments and revoked links. Publishing must remain independent of external delivery failures; retain manual Signal text and PDF exports. Automatic delivery on publication requires explicit approval.

## Ideas for later consideration

Volunteer accounts and swaps, attendance tracking, email/SMS, public schedule viewing, multi-church tenancy, and multi-worker or multi-host deployment are uncommitted ideas. Evaluate them only when a demonstrated need and the necessary security and operational decisions exist.
