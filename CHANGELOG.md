# Changelog

All notable changes to the Revel Backend are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.69.0] - 2026-07-09

### Changed
- Runtime upgraded to Python 3.14 (from 3.13); Docker images, CI, and local setup now target 3.14.

### Fixed
- `reset_events` management command now deletes held series passes before tearing down the organization, so the reset no longer fails on lingering pass holds.

## [1.68.0] - 2026-07-09

### Added
- **Series Passes (season tickets)**: organizers can sell a single pass covering every event in an event series, with pro-rata pricing — `price − passed_events × pro_rata_discount` (clamped at 0), purchasable while at least 2 covered events remain.
  - `SeriesPass` product on an event series (price, pro-rata discount, online/offline/free payment methods, visibility, sales window, optional holder cap), linked to one existing ticket tier per covered event; pass purchases consume that tier's capacity.
  - Purchase materializes a per-event ticket for every future covered event, all-or-nothing under capacity locks; online passes use a single Stripe checkout with the price split penny-exact across events, honoring each tier's VAT rate.
  - Public API: `/series-passes/*` (list, quote, checkout, my-passes, PDF and Apple Wallet downloads). Admin API: `/event-series-admin/{series_id}/passes/*` (CRUD, tier links, holders, offline confirmation, cancellation).
  - Organizers can extend a pass to later-added events (endpoint or inline `series_pass_links` on event create/update); active holders get the new event's ticket free of charge.
  - Cancelling a covered event refunds each holder's share of that event; cancelling a whole pass refunds all remaining events and releases capacity. Holders cannot self-cancel individual pass tickets.
  - Pass-level PDF and Apple Wallet pass with the covered-events list; new `series_pass_purchased` / `series_pass_extended` notifications (de/fr/it), replacing per-ticket emails for pass purchases.
  - Attendee/ticket listings can filter by origin (`?source=pass|direct`) and ticket schemas expose the originating `series_pass`.

### Changed
- **Breaking**: check-in endpoint path renamed from `POST /event-admin/{event_id}/tickets/{ticket_id}/check-in` to `.../tickets/{code}/check-in` — the parameter is now a string code accepting either a plain ticket UUID or a `series:<uuid>` series-pass QR payload.

### Fixed
- Duplicating an event (and materializing recurring-series occurrences) now shifts `waitlist_cutoff_date` along with the other date fields instead of copying the template's absolute timestamp — copies no longer start with an already-closed waitlist or fail validation when duplicated to an earlier date.

## [1.67.1] - 2026-07-07

### Fixed
- IP-based geolocation no longer returns garbage results or logs parser warnings under concurrent requests: each worker thread now gets its own IP2Location database handle instead of sharing one non-thread-safe file cursor.
- Admins are no longer pinged on Discord when a guest account is created — the new-user notification now fires only for real user signups.

## [1.67.0] - 2026-07-01

### Added
- Branded HTML emails for messages that were previously plain-text only: guest RSVP and ticket confirmations, attendee invoice / credit-note / platform-fee / referral-payout delivery emails, and the scheduled revenue report.

### Changed
- **Let's Revel rebrand**: every outbound email and generated PDF document (invoices, credit notes, payout statements, tickets, revenue/VAT report) restyled to the new Let's Revel brand — purple→crimson gradient with the logo header on emails, and the Nata Sans typeface embedded in PDFs. Tickets keep the organizer's own colors, adding only a subtle "Powered by let's revel." footer.
- Email sender display name is now `Let's Revel <revel@letsrevel.io>` (address unchanged).

### Fixed
- The GDPR data-export "failed" notifications (to the user and to admins) now render as proper branded HTML instead of an unstyled, envelope-less fragment.

## [1.66.1] - 2026-06-29

### Fixed
- Recurring events now honor the recurrence rule's timezone, generating DST-correct occurrences; a data migration re-anchors existing non-UTC series so their times no longer drift across daylight-saving boundaries.
- The dashboard RSVP list can now be filtered by multiple statuses at once.
- Telegram: banned users are blocked from interacting with the bot, and the bot's `/unsubscribe` now actually stops notifications instead of leaving per-type settings still delivering.
- Financial documents are no longer silently lost: payout statements and attendee invoices record when their email was sent, surface delivery errors in the admin, retry undelivered emails via a sweep, and mark documents with no possible recipient as terminally undeliverable instead of re-sending forever.
- A failure sending one referrer's payout statement or email no longer aborts the rest of the payout batch.

### Security
- Bumped `python-engineio` and `python-socketio` to clear newly disclosed CVEs.

## [1.66.0] - 2026-06-23

### Added
- **`bootstrap_admin` management command**: an interactive first-run command for self-hosted instances that creates the initial superuser — with its `username` pinned to the email, matching accounts created through the public API — and the first organization (proposing an editable slug), then prints the admin-panel and frontend URLs derived from settings (`BASE_URL`/`ADMIN_URL`/`FRONTEND_BASE_URL`). Preferred over the stock `createsuperuser`, which lets username and email diverge.

## [1.65.0] - 2026-06-22

### Added
- `is_open_ended` flag on events: organizers can mark an event as having no fixed end. `end` is still set (defaulting to start + 24h) and used internally for ICS generation, Wallet expiry, and the "upcoming"/"event ended" gates; the flag is a display signal for the frontend (e.g. render "Ongoing", hide the end time). Settable on events and on recurring-series templates, where it propagates to existing occurrences.

### Security
- `revenue_report_cadence` is now owner-only on organization updates. It was writable via `PUT /organization-admin/{slug}` under the staff-grantable `edit_organization` permission; a non-owner attempting to change the cadence now gets a `403` (no-op submissions of the current value still pass).

## [1.64.1] - 2026-06-22

### Added
- French (`fr`) is now an accepted value for the user's preferred language in the profile and language-update endpoints, matching the languages already configured for the site.

## [1.64.0] - 2026-06-21

### Added
- **Revenue & VAT reporting**: a full financial-reporting suite for organizers, all driven by one tax-precise engine so every view reconciles.
  - Downloadable **revenue & VAT report** (org-admin `POST /organization-admin/{slug}/revenue-report`, poll via `GET /organization-admin/{slug}/revenue-reports/{id}`): a ZIP bundling an XLSX (Summary + Transactions) and a PDF (per-VAT-rate table, refunds, net taxable turnover). Aggregates online (Stripe) and offline/at-the-door payments per currency and per VAT rate; refunds attributed to the period they occurred. Cached and reused unless `?refresh=true`.
  - **Scheduled delivery**: per-org `revenue_report_cadence` (`NONE`/`QUARTERLY`/`MONTHLY`) emails the just-closed period's report ZIP to the org `billing_email` and owner; computed in the org's timezone, skips empty periods.
  - **Live org financials** endpoint `GET /organization-admin/{slug}/revenue`: per-event revenue, sortable (`sort=revenue|event_start`), period-filtered (`year` + optional `month`/`quarter`), with currency switching (`available_currencies`, `?currency=`).
  - Per-event revenue endpoint now reports VAT detail and tracks both online and offline ticket refunds.
- **Self-hosting support**: the backend now boots and runs on a self-hosted box without ClamAV, Telegram, or the full geo dataset, with feature flags to tailor a deployment.
  - `FEATURE_MALWARE_SCAN` (default on) — when off, uploads skip the ClamAV scan and are marked clean.
  - `FEATURE_TELEGRAM` (default on) — when off, Telegram delivery is dropped and the linking endpoints 404.
  - `FEATURE_ORGANIZATION_CREATION` (default on) — when off, `POST /organizations/` returns 403 for non-staff (single-org instances); staff/superusers bypass.
  - Cities-CSV fallback to a bundled `worldcities.mini.csv` so a fresh container no longer crash-loops on the city-load migration.
  - `provision_stripe_webhooks --format json` for machine-readable webhook setup, and a new self-hosting documentation section.
- **Feature flags in `GET /version`**: the anonymous version endpoint now returns a `features` object (`organization_creation`, `telegram`, `google_sso`, `llm_evaluation`) so clients can hide gated UI instead of letting users hit a 403/404.
- **Event schedule / timeline**: organizers can author an ordered list of display-only sessions (title, markdown description, relative start offset, optional duration/location, `is_required` badge) via `PUT /event-admin/{event_id}/schedule`; exposed on the event detail and copied verbatim on event duplication/recurrence.
- **Scheduled announcements**: send an announcement at a future absolute time or relative to event start/end (auto-shifts when the event moves); adds a `SCHEDULED` status plus `/schedule` and `/unschedule` endpoints, delivered by a background sweep.
- **Resend announcements to new sign-ups**: an event announcement can be re-delivered to attendees who join after the first send, until the event ends, without double-notifying.
- **Immediate transactional emails**: payment- and ticket-related notifications (`PAYMENT_CONFIRMATION`, `TICKET_CREATED`, `TICKET_CANCELLED`, `TICKET_REFUNDED`) now bypass the digest and deliver immediately on the user's enabled channels.
- Event `timezone` (resolved IANA string, UTC fallback) exposed on the event list and detail schemas, and an `audience` descriptor on the public announcement schema.
- Admin moderation: staff can now delete offensive food items, and offensive food-item names are blocked at creation across all languages (new `moderation` app).
- Admin ticket list is now sortable, and shows the discount code applied to each ticket.
- Questionnaire submissions now expose `requires_evaluation`.

### Changed
- Email digest overhauled: grouped by human-readable type label, each item carrying a body, timestamp, and a "View details" link, restyled to match the transactional emails.
- Observability flag renamed `ENABLE_OBSERVABILITY` → `FEATURE_OBSERVABILITY`; the old env var still works as a deprecated alias for one release.
- **Breaking** (per-event revenue endpoint `GET /event-admin/{event_id}/revenue`): now returns `EventFinancialsSchema` — `refunded` renamed to `refunds`, `paid_ticket_count` removed (derive from `sold_count - refunded_count`, and `sold_count` now also counts later-refunded/cancelled sales), and VAT detail (`net_taxable`, `vat`, `rate_buckets`) added. Requires a coordinated frontend update.

### Fixed
- Event times in notifications and emails are now always rendered in the event's own timezone.
- New ticket tiers now appear at the bottom of the list instead of the top.
- Questionnaire/poll shuffling is now stable per viewer across page reloads (still varies between users) instead of re-randomizing on every load.
- Creating a discount code with a duplicate code now returns a clear 409 instead of an opaque 500.
- Discount codes that have never been used are hard-deleted; used codes are deactivated to preserve history.
- Reduced redundant 4xx log noise while ensuring 500 tracebacks are captured.

### Removed
- The on-check-in notification (`TICKET_CHECKED_IN`) is no longer sent — the holder is already at the event.

## [1.63.1] - 2026-06-13

### Fixed
- `SiteSettings`/`Legal` singletons (django-solo) are now cached, eliminating a per-request N+1 (one request was issuing 160+ identical `SiteSettings` queries) that made status-update and bulk-notification flows slow.

### Security
- Bumped `tornado` to 6.5.7 to clear CVE-2026-49854.

## [1.63.0] - 2026-06-11

### Added
- **Stripe webhook hardening**: multiple signing secrets (`STRIPE_WEBHOOK_SECRETS`, CSV) so secrets can be rotated without downtime; a `StripeWebhookEvent` log providing DB-level idempotency against redeliveries; explicit event dispatch; and a daily pruning job. Invalid signatures now fail closed with a 403 instead of a 500.
- Stripe API version pinned (`STRIPE_API_VERSION`) for predictable behaviour, with a `charge.refunded` compatibility fix.
- `provision_stripe_webhooks` management command to set up Stripe webhooks.
- The platform host's own Stripe account can be bound to a single organization (superuser admin actions), enabling the host to sell tickets directly.

### Fixed
- Duplicate Stripe webhook deliveries now correctly replay idempotent task dispatches instead of being silently dropped.
- Organization admin now exposes the social-media fields from `SocialMediaMixin`.

## [1.62.7] - 2026-06-10

### Fixed
- `get_cached_user_location` no longer caches the volatile IP-derived location fallback; only the stable city-preference lookup is cached. Previously, a volatile IP (VPN, mobile, hotel) could pin "Nearest First" event sorting to the wrong city for up to one hour.

## [1.62.6] - 2026-06-10

### Fixed
- `download_ip2location` now correctly extracts the `.BIN` database from the ZIP archive delivered by ip2location.com; the file was previously saved as raw ZIP bytes, causing every IP lookup to raise `struct.error` (silently swallowed), so "Nearest First" event sorting never worked in production.

## [1.62.5] - 2026-06-10

### Fixed
- `loki_logs.py` `--status-code` and `--user-id` flags now use structured metadata label filters, matching the single-render JSON format introduced in 1.62.4; they previously line-grepped the old double-rendered blob and returned no results.
- `user_id` is now bound to the structlog context on successful JWT authentication (`I18nJWTAuth`, `OptionalAuth`), so all log lines emitted during an authenticated API request — including `request_finished` and unhandled-exception logs — are attributable to the user and filterable in Loki via `--user-id`.

## [1.62.4] - 2026-06-10

### Fixed
- Client IP is now resolved from `X-Real-IP` (set by Caddy from the Cloudflare-resolved visitor IP) across request logging, geolocation middleware, and impersonation audit; the previous `X-Forwarded-For`-first derivation was incorrect for this reverse-proxy stack.
- Structlog JSON was double-rendered: the outer wrapper's `event` field contained the real log as an escaped JSON string, so `status_code` and `user_id` were never filterable in Loki. Logs are now single-render JSON.

## [1.62.3] - 2026-06-10

### Added
- `scripts/loki_logs.py` — operator CLI for querying production logs from Loki via Grafana's datasource-proxy API; supports filtering by service, log level, status code, user ID, request/trace ID, and grep patterns without exposing Loki publicly.

### Fixed
- Celery worker logs are now single-line structlog JSON (`CELERY_WORKER_HIJACK_ROOT_LOGGER = False`); previously Celery's text banner wrapped each log line, breaking JSON parsing and mislabelling stream levels in Loki.
- `generate_thumbnails` management command no longer enqueues thumbnail tasks for non-image uploads (audio, video, documents), eliminating a persistent flood of `ThumbnailGenerationError` failures in the Celery results backend.

### Security
- `GET /api/questionnaires/{id}` had no permission check, allowing any authenticated user to read the full admission answer key (`is_correct`, weights, `min_score`, `reviewer_notes`, `llm_guidelines`) for any public or unlisted organization's questionnaire. Now requires org admin permission, matching sibling routes.
- `GET /api/questionnaires/` was similarly unscoped; results are now limited to organizations the caller administers.
- Multiple-choice admission-gate bypass: selecting every option for a single-answer question scored full marks and auto-approved `AUTOMATIC` gates, because `allow_multiple_answers=False` was enforced only in `clean()` which `bulk_create` skips. Submissions now reject more than one option on single-answer questions at the service layer.
- DRAFT poll detail endpoint no longer leaks unpublished questions and options to any user knowing the UUID when visibility was `PUBLIC` or `UNLISTED`; hidden from non-staff until the poll is published.

## [1.62.2] - 2026-06-09

### Fixed
- Event-series notification links pointed at the admin route (`/org/{slug}/series/{slug}`) instead of the public page (`/events/{slug}/series/{slug}`), producing dead links in the "series followed" (staff) and "series events generated" (follower) notifications. They now resolve to the correct public series URL.

## [1.62.1] - 2026-06-08

### Added
- Referral admin now supports full create / edit / delete, so staff can manually record a referral win when the referred user never used the referrer's link. `referrer` stays auto-derived from the referral code; double-referrals and referrals that already have payouts remain protected.

### Security
- Patched dependency CVEs: Django `5.2.14` → `5.2.15` (stays on the 5.2 LTS line), PyJWT `2.12.1` → `2.13.0`, and pip `26.1.1` → `26.1.2`.

## [1.62.0] - 2026-06-02

### Added
- **Event Cancellation Reason**: organizers can attach an optional reason when cancelling an event
  - Accepted on `POST /actions/update-status/{status}` (up to 1000 chars) only when transitioning to `cancelled`; automatically cleared when un-cancelling so a stale reason can't resurface on a later re-cancel
  - Included in the `EVENT_CANCELLED` notification (in-app / email / Telegram) and returned on event list + detail responses
  - Readable only by attendees — ticket holders, confirmed `YES` RSVPs, and event staff/owners; merely-invited or anonymous users receive `null`

### Changed
- German (`de`) translations refreshed — standardized colon gendering and harmonized terminology

### Fixed
- Event-cancellation notifications were never delivered: a missing `refund_available` context key caused the validator to silently reject every `EVENT_CANCELLED` notification (in-app / email / Telegram). They now send correctly, including refund availability.

## [1.61.0] - 2026-05-28

### Added
- **Questionnaire & Poll Duplication**: organizers can deep-copy a questionnaire or a poll as a starting point instead of rebuilding every question by hand
  - `POST /api/questionnaires/{org_questionnaire_id}/duplicate` clones an organization questionnaire — all sections, questions, options, and their intra-questionnaire dependencies — into a new **DRAFT** questionnaire in the same organization; pass `copy_associations: true` to also link the copy to the template's events and event series (unattached by default). Requires `create_questionnaire` on the organization
  - `POST /api/polls/{poll_id}/duplicate` clones a poll and its wrapped questionnaire into a new **DRAFT** poll, copying visibility, result timing, anonymity, vote-change policy, and membership-tier restrictions while resetting the lifecycle (status and open/close timestamps). `staff_anonymous`/`public_anonymous` — normally immutable after creation — may be overridden on the copy. Requires `manage_polls` on the organization
  - Votes, submissions, answers, evaluations, and uploaded files are never copied

### Security
- Organization-admin resource reads (`GET /organization-admin/{slug}/resources` and the by-id endpoint) now require organization staff; previously any authenticated user could read another organization's private/staff-only resources and their signed file URLs
- Questionnaire submissions now reject multiple-choice options that don't belong to the question they're paired with, closing an admission-gate bypass that let crafted option IDs inflate the evaluation score into an auto-approval
- Anonymous rate limiting now trusts only the proxy-supplied client IP (`NUM_PROXIES`, default `1`), so a forged `X-Forwarded-For` header can no longer evade login, registration, and other anonymous throttles
- Hard-blacklisted users can no longer reclaim a staff-granting organization invitation token to regain access after being banned
- Quarantined (malware-flagged) uploads are now stored behind the protected, signed-URL media path instead of being publicly downloadable from `/media/`; staff download them via a signed link in the admin
- Event-update notifications no longer disclose a changed venue address (old or new value) to recipients not permitted to see the event's address
- The Telegram "Join Waitlist" action now applies the same event-visibility scoping and full eligibility check as the web API, so users can no longer waitlist (and reserve capacity on) events they aren't eligible for or can't see

## [1.60.0] - 2026-05-26

### Added
- **Event Bookmarks**: users can privately bookmark events to find them again later, without signing up or affecting eligibility
  - New `POST`/`DELETE /api/events/{event_id}/bookmark` endpoints — bookmarking returns `201` for a new bookmark or `200` if it already existed; unbookmarking is idempotent and works even for events you can no longer see
  - New `is_bookmarked` field on event list and detail responses
  - New `bookmarked` facet on `GET /api/dashboard/events`; bookmarked **unlisted** events surface here even though they stay hidden from discovery listings
  - Searchable **Bookmarks** section in the admin

### Fixed
- Concurrent identical "get-or-create" requests (e.g. rapidly clicking bookmark, or racing tag / food-item / billing-profile creation) no longer risk a 500: the shared race-protection helper now also recovers from the `ValidationError` that `full_clean()` raises when a competing row was committed first, not just `IntegrityError`

## [1.59.0] - 2026-05-25

### Added
- **Polls**: a new `polls/` app wrapping the questionnaire pipeline with poll-specific concerns (audience, lifecycle, anonymity, result visibility). A `Poll` is 1:1 with a `Questionnaire` and votes are stored as `QuestionnaireSubmission` rows
  - New `/api/polls/` endpoint group: list, detail, results, create, patch, open, close, reopen, delete, vote, withdraw
  - New `manage_polls` permission (JSONField-backed, no migration)
  - `polls.close_polls_due` Celery beat task auto-closes due polls every minute (row-locked to serialize close-vs-vote races)
- **Top Organizations by Traction**: admin-dashboard leaderboard of the 10 organizations that reached the most distinct users in the last 12 months (an RSVP of yes/maybe **or** an active/checked-in/pending ticket counts; a user is counted once per organization), shown as a horizontal bar chart plus a ranked table linking to each organization's admin page

### Changed
- Several API errors now return more accurate HTTP statuses: cancelling an already-cancelled ticket returns **409** (was 400), missing billing info returns **422** (was 400), and file-validation errors return **422** (was 400)
- Contact-form notifications delivered over **Telegram** no longer include the message subject or preview (Telegram chats are not end-to-end encrypted); recipients are prompted to open Revel for the full message. Email and in-app notifications are unchanged

### Fixed
- Celery tasks dispatched while handling an HTTP request are now deferred to `transaction.on_commit`, eliminating intermittent `DoesNotExist` errors when a worker picked up a task before the request's transaction had committed
- The nightly `expire_subscriptions_past_grace` task no longer crashes with `InvalidCursorName` under PgBouncer transaction pooling; server-side cursors are disabled when running behind PgBouncer

## [1.58.0] - 2026-05-21

### Added
- **Advanced Waitlist Management**: configurable, batched, time-limited waitlist offers, opt-in per event via `Event.waitlist_time_window`. When capacity opens, a batch of waitlisted users (FIFO or lottery) is offered spots with an expiry deadline, and those spots are reserved during the window so non-waitlist users can't take them
  - New `Event` fields: `waitlist_time_window`, `waitlist_batch_size`, `waitlist_cutoff_date`, `waitlist_cutoff_window`, `waitlist_lottery_mode`
  - New `WaitlistOffer` model with a partial-unique constraint on `(event, user)` for pending offers, plus admin offer-management endpoints
- `EventUserEligibility.reason_code` — stable machine-readable `ReasonCode` enum mirroring the human-readable `reason` string; clients should switch on `reason_code` instead of matching localized prose
- `WaitlistOfferSchema.user` serializes as a nested `MinimalRevelUserSchema` (id, name, email, …) instead of a bare UUID, so admin UIs can render organizers' offers without a follow-up lookup

### Changed
- Admin manual-create and reactivate waitlist-offer endpoints accept a payload-provided `expires_at` even when `event.waitlist_time_window` is not configured globally. Previously both endpoints unconditionally returned 400 in that case; they now only 400 when neither the event nor the payload defines an expiry. The endpoints still default to `now + event.waitlist_time_window` when the global window is set and the payload omits `expires_at`.

### Fixed
- Waitlist processing no longer cascades into auto-reoffering a user whose offer was just revoked. The processor now excludes users with REVOKED offers in addition to PENDING; admin revoke is a soft-skip until the offer is reactivated or a manual offer is issued. EXPIRED users stay eligible (normal batch-timeout lifecycle).

## [1.57.0] - 2026-05-19

### Added
- **Reserved slug tokens**: `Organization` creation is rejected when the name slugifies to a reserved token — a hardcoded set of platform-critical entries (`admin`, `api`, `www`, `revel`, …) unioned with a DB-managed `ReservedSlugToken` soft list (seeded with abuse patterns like `demo`, `test`, `foo`) editable from the Django admin. Enforced in `create_organization`; messages localized in EN/IT/DE

## [1.56.0] - 2026-05-18

### Added
- **Self-Served Email Change**: Authenticated users can change their email via a JWT-confirmation flow
  - `POST /account/email-change-request` requires the current password and sends a single-use confirmation link to the new address (proof of control)
  - `POST /account/email-change-confirm` swaps `email` + `username`, blacklists every outstanding JWT for the user (email is an identity primitive), and returns a fresh token pair so the confirming device stays signed in
  - Both old and new addresses receive completion emails; the old address also receives an in-flight notice with a masked rendering of the new address
  - Google-SSO accounts and same-email or already-taken targets are rejected with clear 400s; globally-banned targets silent-no-op (mirrors verification flow)

## [1.55.0] - 2026-05-18

### Added
- `is_recurring: bool` on `EventSeriesInListSchema` and `EventSeriesRetrieveSchema`, resolved via a single subquery annotation (no N+1)

### Fixed
- Event/organization invitation tokens created with `duration=0` ("Never expires" in the frontend) now correctly persist `expires_at=None` instead of `timezone.now() + timedelta(0)` (effectively expiring immediately)
- Unhandled exceptions in the API, the Django admin, and middleware now emit full tracebacks to Loki — previously several layers were silently dropping `exc_info`, leaving production 500s un-debuggable

### Changed
- `cleanup_email_logs` Celery task is now scheduled daily at 04:30 UTC

## [1.54.0] - 2026-05-15

### Added
- `GET /organization-admin/{slug}/plans` — list every membership-subscription plan across an organization in one request, with optional `is_active` filter
- `tier_name` resolved on `PlanSchema` so admin UIs can render the plan-to-tier link without a client-side join

## [1.53.0] - 2026-05-15

### Added
- **Membership Subscriptions — Phase 1 (OFFLINE)**: Staff-managed recurring memberships with full lifecycle
  - `MembershipSubscriptionPlan`, `MembershipSubscription`, and `MembershipPayment` models with denormalized organization, partial-unique constraint on non-terminal status per `(user, org)`, and `dateutil.relativedelta` period arithmetic (handles month-end edges)
  - New `manage_subscriptions` permission (owner ✓, staff ✓ default, member ✗)
  - Staff endpoints: plan CRUD per tier, subscription create/list/get + lifecycle (cancel/pause/resume), record payment, record-only refund
  - Member endpoints: `GET /me/membership-subscriptions`, `GET /me/organizations/{org_id}/subscription`
  - `post_save` signal syncs `OrganizationMember.status + tier` from the subscription; never creates members, leaves `BANNED` alone
  - Daily Celery beat task `events.expire_subscriptions_past_grace` transitions `ACTIVE → PAST_DUE → EXPIRED` past `Organization.membership_grace_period_days` (default 7)
  - New `Organization.membership_grace_period_days` and `Organization.membership_refund_policy` fields
- `GET /organization-admin/{slug}/subscriptions/{sub_id}/payments` — paginated payment history per subscription (newest first)
- `occurred_at` on `MembershipPaymentSchema` lets staff backfill OFFLINE payments with the real payment date; anchors `period_start` / `period_end` math when set
- `GET /me/memberships` — unified, paginated endpoint that surfaces every `OrganizationMember` for the caller, inlining the most recent non-terminal subscription per org when one exists
- `organization_name`, `organization_slug`, and `organization_logo_url` resolved on `MySubscriptionSchema` so member dashboards can render a usable card without N+1 org lookups
- `GET /organization-admin/{slug}/event-series/{series_id}/template-event` — admin endpoint that returns the `EventDetailSchema` for a series' template event (the public detail route filters out templates by design)

### Security
- Bumped `urllib3` to patch a transitive vulnerability

## [1.52.3] - 2026-05-11

### Fixed
- `TicketTierUpdateSchema`, `DiscountCodeCreateSchema`, and `DiscountCodeUpdateSchema` now validate `currency` against the same supported list (`Currencies` whitelist) already enforced on create; previously clients could set unsupported currencies via update or discount endpoints, bypassing the ECB/frankfurter rate coverage

## [1.52.2] - 2026-05-10

### Security
- Bumped Django to 5.2.14

## [1.52.1] - 2026-05-08

### Added
- **Organization Contact Method**: Each org can now choose `none` / `email` / `form` for inbound contact
  - `Organization.contact_method` field; `email` and `form` require a verified `contact_email`
  - `POST /organizations/{slug}/contact` — public contact endpoint (verified-email auth, throttled). Submission is delivered as a single transactional email to the org's verified mailbox with `From: noreply@`, `Reply-To: <sender>`; staff also receive an in-platform `ORG_CONTACT_MESSAGE_RECEIVED` notification (default `IN_APP + TELEGRAM`, gated on `edit_organization`)
  - `contact_email` resolves on public org schemas only when `contact_method=email` and the address is verified; `contact_email_verified` removed from public surfaces (still visible to admins)
  - Changing `contact_email` to a new (unverified) address automatically forces `contact_method=none` so the resolver invariant holds

### Fixed
- In-app, Telegram, and email templates for `ORG_CONTACT_MESSAGE_RECEIVED` now render the full message body (sender, subject, preview, admin link); previously only the title showed because channel templates were missing

## [1.51.0] - 2026-04-28

### Changed
- Admin-panel authorization tightened: "Impersonate user" is now restricted to superusers; sensitive organization fields are read-only for non-superuser staff

## [1.50.1] - 2026-04-28

### Fixed
- `notify_organization_created` is now editable and visible in the `SiteSettings` admin (the field existed on the model but was missing from the admin `fieldsets`)

## [1.50.0] - 2026-04-25

### Added
- **Recurring Events**: First-class recurring event series with rolling-window materialization
  - `RecurrenceRule` model — frequency, interval, weekdays, monthly params, boundaries; emits RFC 5545 `RRULE` strings via `python-dateutil`
  - `EventSeries` extensions: `template_event`, `recurrence_rule`, `exdates`, `auto_publish`, `generation_window_weeks`, scheduling control
  - Each occurrence is a real materialized `Event` (independent tickets, payments, capacity) duplicated from the template, with `PROPAGATABLE_FIELDS` whitelist for safe template edits
  - 7 new admin endpoints: create recurring series, edit template with propagation, update recurrence, cancel occurrence, manual generate, pause/resume
  - Daily Celery beat task drives the rolling-window generation
  - Single digest `SERIES_EVENTS_GENERATED` notification per batch instead of one notification per generated occurrence
  - `GET /organization-admin/{slug}/event-series/{series_id}` returns the full admin `EventSeriesRecurrenceDetailSchema`
  - `GET /organization-admin/{slug}/event-series/{series_id}/drift` returns the IDs of future occurrences that no longer match the current `RRULE` (cadence-change drift), excluding manually-modified and cancelled ones
- **User-Initiated Ticket Cancellation & Refunds**: Per-tier opt-in self-service cancellation with snapshotted refund policy
  - `TicketTier.allow_user_cancellation`, `cancellation_deadline_hours`, and `refund_policy` (tiered % by hours-before-event + flat fee)
  - `refund_policy_snapshot` immutably written onto each `Ticket` at purchase time so policy changes do not retroactively affect issued tickets
  - `GET /events/tickets/{id}/cancellation-preview` — refund-policy windows + live quote for the caller's ticket
  - `POST /events/tickets/{id}/cancel` — self-service cancel for free, offline, at-the-door, and online (Stripe) tickets. Returns `409 CancellationBlockedErrorSchema` with a stable `code` when not permitted (already cancelled, checked-in, event started, past deadline, etc.)
  - Public `TicketTierSchema` now exposes `allow_user_cancellation`, `cancellation_deadline_hours`, and `refund_policy` so buyers see the cancellation terms before purchase
  - `Payment.refund_amount`, `refund_status`, `refunded_at`, `stripe_refund_id`, and `refund_failure_reason` fields
  - `Ticket.cancelled_at`, `cancelled_by`, `cancellation_source`, and `cancellation_reason` fields

### Changed
- Stripe `charge.refunded` webhook now matches refunds per-`Payment` via a 5-branch strategy (existing `stripe_refund_id`, metadata `ticket_id`, unambiguous exact amount, full-intent sweep, ambiguous → log-only) — previously cascaded across all payments sharing a `PaymentIntent`
- `TICKET_CANCELLED` notifications now branch headline copy on `cancellation_source` (user-initiated / organizer / stripe_dashboard) across in-app, Telegram, and email
- Refund notifications now report `payment.refund_amount` when set, instead of the full `payment.amount` (fixes partial-refund UX lies)

### Fixed
- Stripe Connect refunds no longer fail with 502: `_issue_stripe_refund` now passes `stripe_account=org.stripe_account_id` for direct charges
- Admin `cancel` and `mark-refunded` actions now record `cancelled_at`, `cancelled_by`, `cancellation_source=ORGANIZER`, and optional `cancellation_reason`; `mark_ticket_refunded` also populates `payment.refund_amount`, `refund_status=SUCCEEDED`, and `refunded_at`
- Admin `POST/PUT /ticket-tier` accepts a nested `refund_policy` end-to-end (previously `Decimal` leaves in the nested JSON caused `full_clean()` to raise)
- Recurring-event tier duplication previously dropped `allow_user_cancellation`, `cancellation_deadline_hours`, `refund_policy`, `seat_assignment_mode`, `max_tickets_per_user`, `vat_rate`, `display_order`, `restrict_visibility_to_linked_invitations`, and `restrict_purchase_to_linked_invitations` on every generated occurrence — `_duplicate_ticket_tiers` now introspects `_meta.concrete_fields` with an explicit exclusion list

## [1.48.1] - 2026-04-17

### Fixed
- Unbanning a user now clears the stale `BANNED` `OrganizationMember` row that the ban created. The membership transitions to `CANCELLED` (not `ACTIVE`, to avoid implicitly re-granting prior membership privileges), so the org and its events become visible again. If another blacklist entry still covers the user in the org, the `BANNED` status is preserved.

## [1.48.0] - 2026-04-15

### Added
- Discord admin-ping channel (`DISCORD_ADMIN_WEBHOOK_URL`) for new-user and new-organization events. The new-user Discord ping is PII-free (count only, skipped for guest signups); the new-organization Discord ping includes org name + owner email
- `SiteSettings.notify_organization_created` flag to gate new-organization admin pings
- New-user Pushover ping now includes the referrer's email (when present) and a running user count; new-organization Pushover ping includes owner email + running org count

### Fixed
- Event duplication now copies `address_visibility`, `requires_full_profile`, `public_pronoun_distribution`, `max_tickets_per_user`, `venue`, `location_maps_url`, and `location_maps_embed`; potluck items are duplicated as host suggestions (no assignee/created_by)
- Daily exchange-rate fetch no longer crashes: switched to `https://api.frankfurter.dev/v1` (the old URL was returning 301 and breaking the Celery task)
- `first_name`, `last_name`, and `preferred_name` whitespace is now stripped at save time (and via `StrippedString` in `ProfileUpdateSchema`)
- `EventSeries` notification suppression during recurring-event batch materialization is now thread-safe: replaced a thread-unsafe signal disconnect/reconnect with `ContextVar`-based suppression so concurrent requests no longer interfere with each other's notifications

## [1.47.0] - 2026-03-31

### Added
- **Attendee Invoicing**: Organizations can now generate invoices for ticket buyers on their behalf, with three modes: NONE (default), HYBRID (draft + manual issue), and AUTO (generate + send immediately)
- VAT preview endpoint for checkout (`POST /events/{event_id}/tickets/vat-preview`) with buyer-specific VAT calculation and VIES caching (Redis, 30-minute TTL)
- `AttendeeInvoice` and `AttendeeInvoiceCreditNote` models with per-org sequential numbering
- Buyer-facing invoice listing and download endpoints (`/dashboard/invoices`)
- Org admin endpoints for managing attendee invoices: list, detail, edit draft, issue, delete draft, download
- Credit notes auto-generated on Stripe refund webhooks; draft invoices deleted on refund

## [1.46.0] - 2026-03-25

### Added
- **UNLISTED Visibility**: Organizations and events can now be set to UNLISTED — accessible via direct link but hidden from browse/search listings

### Fixed
- Timezone formatting in event displays
- `depends_on_option` validation on questionnaire questions
- Resource org scoping to prevent cross-org resource access

### Changed
- Platform fees switched to VAT-exclusive semantics (VAT added on top of fee rather than extracted from gross)

## [1.45.1] - 2026-03-24

### Fixed
- Ticket PDF and Apple Wallet pass generation now uses optimized images, reducing file size and generation time

## [1.45.0] - 2026-03-24

### Added
- **Fine-Grained Tier Restrictions**: Ticket tiers can now restrict visibility and purchase access to users whose invitation links to that specific tier (`restrict_visibility_to_linked_invitations`, `restrict_purchase_to_linked_invitations`)
- Invitations now support a many-to-many `tiers` field for tier-linked access
- `public_pronoun_distribution` flag on events — controls whether pronoun distribution statistics are visible to non-staff attendees

## [1.44.1] - 2026-03-23

### Fixed
- Follower notifications (`NEW_EVENT_FROM_FOLLOWED_ORG`/`NEW_EVENT_FROM_FOLLOWED_SERIES`) no longer sent for non-public events

## [1.44.0] - 2026-03-23

### Added
- **Referral System — Full Launch**: Complete referral program with Stripe auto-payouts
  - `UserBillingProfile` model for referrer billing details (VAT ID, address, self-billing agreement)
  - Referrer Stripe Connect onboarding endpoints
  - Monthly payout calculation task aggregating net platform fees per referral
  - Stripe auto-transfers with self-billing invoice (Gutschrift) for B2B referrers or payout statement for B2C
  - Minimum payout threshold with automatic rollover of below-threshold amounts
  - User-facing payout listing and statement download endpoints
- Dashboard hides invitations for events the user has already accepted
- Audio file uploads now support `.m4a` format
- DiscountCode registered in Django admin panel
- Hourly periodic task to flush expired JWT tokens

### Fixed
- Referral payouts skip processing when billing profile has empty required fields

### Changed
- Events sorted newest-first in admin panel
- Fixed reverse import dependency in admin module

## [1.43.0] - 2026-03-20

### Added
- Expired or fully-used invitation tokens now return **410 Gone** with distinct messages ("expired" vs "used up") instead of 404, enabling contextual frontend guidance
- Single-answer questionnaire questions can now have multiple options marked as correct — selecting any correct option earns full points

### Fixed
- Questionnaire retake allowed immediately when `can_retake_after` is None or zero
- `check_in_starts_at` and `check_in_ends_at` exposed on `EventDetailSchema`
- Billing endpoints now return 422 (not 500) when `billing_address` or `billing_email` is null

## [1.42.3] - 2026-03-19

### Added
- **Referral System — Foundation**: `ReferralCode` and `Referral` models, admin registration, referral settings (revenue share %, min payout, payout day)

### Fixed
- `billing_name` field added to organizations for legal entity invoicing (auto-populated from VIES when available)

## [1.42.2] - 2026-03-17

### Fixed
- `max_attempts` field added to questionnaire create schema (was missing, causing validation errors)
- Questionnaire update schema refactored for consistency

## [1.42.1] - 2026-03-17

### Fixed
- `location_maps_url` max length increased from 200 to 2048 to accommodate long Google Maps URLs

## [1.42.0] - 2026-03-11

### Added
- **Full VAT Support**: Complete VAT handling for ticket sales and platform fees, with per-tier VAT rate overrides, EU B2B reverse charge, and VIES integration
- `requires_evaluation` flag on `OrganizationQuestionnaire` — allows questionnaires that collect data without gating access
- Invitation emails now use the event's custom `invitation_message` with safe string interpolation (`{user_name}`, `{event_name}`, etc.)
- Pending invitees (unregistered emails) now receive notification emails with event details

### Fixed
- `ticket_tier_id` made optional on event tokens for ticketed events (was incorrectly required)
- Maximum text answer length bumped from 500 to 1000 characters

## [1.41.2] - 2026-03-10

### Fixed
- Cancelled tickets no longer count toward the eligibility check that skips re-evaluation for existing ticket holders

## [1.41.1] - 2026-03-08

### Changed
- Event duplication now uses date as slug suffix (e.g., `my-event-2026-03-08`) instead of sequential numbers

### Fixed
- XLSX export generation fixed (incorrect column mapping)

## [1.41.0] - 2026-03-07

### Added
- **XLSX Exports**: Event attendees, ticket holders, and member lists can now be exported as `.xlsx` spreadsheets

## [1.40.0] - 2026-03-05

### Added
- Event list filtering by `requires_ticket` flag

## [1.39.0] - 2026-03-05

### Added
- **Discount Codes**: Organizations can create discount codes with percentage or fixed-amount discounts, scoped to events/series/tiers, with usage limits and validity windows
- Feature flags for LLM-based questionnaire evaluation (`FEATURE_LLM_EVALUATION`) and Google SSO (`FEATURE_GOOGLE_SSO`)

## [1.38.5] - 2026-03-03

### Security
- Tightened staff management: prevent privilege escalation via staff self-promotion
- Hardened guest user checkout flow validation

## [1.38.4] - 2026-02-28

### Security
- Additional security patches and minor cleanup

## [1.38.3] - 2026-02-26

### Fixed
- Logging hotfix for production stability

## [1.38.2] - 2026-02-26

### Changed
- Improved logging infrastructure with structured JSON output and context enrichment

### Security
- Added Bandit (Python security linter) checks to CI pipeline

## [1.38.1] - 2026-02-25

### Fixed
- Guest users now receive an email activation link (instead of automatic activation) when requesting a password reset

## [1.38.0] - 2026-02-25

### Added
- **Global Banning System**: Platform-wide bans by email, domain, or Telegram username — auto-deactivates matching users with signal-based enforcement
- Codecov.io integration for test coverage tracking

## [1.37.3] - 2026-02-23

### Security
- XSS defense-in-depth hardening across notification templates and user-generated content rendering

## [1.37.2] - 2026-02-23

### Fixed
- Better traceback logging in failed notification tasks
- Improved HTML escaping in notification content

### Changed
- Bumped transitive dependencies

## [1.37.1] - 2026-02-23

### Fixed
- Newline handling in Telegram system notifications

## [1.37.0] - 2026-02-22

### Added
- **System Announcements**: Platform administrators can broadcast announcements to all active users via a dedicated admin UI, with optional guest inclusion and batched async delivery

## [1.36.3] - 2026-02-22

### Fixed
- Dropped `.iterator()` in ticket file cache cleanup task (was causing issues with queryset evaluation)

## [1.36.0] - 2026-02-16

### Added
- **Ticket File Caching**: generated PDF and pkpass files are now persisted on the Ticket model with content-hash-based invalidation, avoiding regeneration on every download
- PDF download endpoint (`GET /tickets/{id}/pdf`) with signed-URL redirects
- Daily cleanup task for cached ticket files of past events
- MkDocs documentation site

### Changed
- Replaced OpenAI SDK with Instructor's `from_provider()` for vendor-agnostic structured LLM output in questionnaire evaluation
- Consolidated evaluator class hierarchy from 6 classes to 3 with dedicated LLM settings module

## [1.35.1] - 2026-02-15

### Fixed
- Follower notifications (`NEW_EVENT_FROM_FOLLOWED_ORG`/`NEW_EVENT_FROM_FOLLOWED_SERIES`) now receive email notifications (previously restricted to in-app only)
- `display_order` field exposed on `TicketTierSchema` with proper ordering

## [1.35.0] - 2026-02-14

### Added
- `price_paid` override support for PWYC ticket confirmation and check-in endpoints
- `ConfirmPaymentSchema` for typed optional `price_paid` payload on confirm endpoint

### Changed
- Removed dead `TicketService` class, `EventManager.create_ticket()`, and related methods (all traffic uses `BatchTicketService`)

## [1.34.4] - 2026-02-13

### Fixed
- Telegram authorization bypass edge case

## [1.34.3] - 2026-02-13

### Fixed
- Apple Wallet pass layout: venue name as field label with address as value, compact date header includes time, org name as primary field label

## [1.34.2] - 2026-02-13

### Added
- Expiry date on Apple Wallet passes

## [1.34.1] - 2026-02-13

### Fixed
- Apple Wallet pass address and price resolution
- Apple Wallet pass date display formatting

## [1.34.0] - 2026-02-13

### Added
- **Questionnaire Summary Endpoint**: `GET /{org_questionnaire_id}/summary` returning submission counts by status, score statistics (avg/min/max), and multiple-choice answer distributions with optional event/series filtering
- **Per-Event Questionnaire Scoping**: Questionnaires can now be scoped to specific events
- **Ticket Tier Ordering**: Support for ordering ticket tiers

## [1.33.0] - 2026-02-11

### Added
- Safe reboot check endpoint for zero-downtime deployments

## [1.32.0] - 2026-02-10

### Added
- **Maintenance Banner**: Configurable maintenance banner on `SiteSettings` with message, severity, `scheduled_at`, and `ends_at` fields
- Banner exposed via `GET /version` as optional `banner` object (null when inactive)
- Banner rendered on admin dashboard with severity-based styling
- Automatic hiding when `maintenance_ends_at` is in the past

## [1.31.13] - 2026-02-10

### Fixed
- Telegram delivery channel now automatically added to notification preferences when user links their Telegram account

## [1.31.12] - 2026-02-08

### Fixed
- Potluck list queryset now uses `include_past=True` to show potluck items for past events

## [1.31.11] - 2026-02-05

### Fixed
- Telegram messages longer than 4096 characters are now truncated to stay within Telegram API limits

## [1.31.10] - 2026-02-05

### Fixed
- At-the-door ticket payments now treated as RSVPs
- Telegram notification titles properly escaped to avoid formatting issues

## [1.31.9] - 2026-02-04

### Changed
- Updated dependencies and removed unused packages

## [1.31.8] - 2026-02-04

### Added
- Organization admin list view now shows event count, member count, and Stripe connection status

## [1.31.7] - 2026-02-03

### Added
- Atomic create-and-claim potluck endpoint for creating and claiming a potluck item in a single request

### Changed
- Consolidated Celery tasks into fewer modules for better organization

### Fixed
- Local ClamAV Docker image configuration

## [1.31.5] - 2026-02-01

### Added
- Improved GDPR user data export using `ProtectedFile` with automatic cleanup task

## [1.31.4] - 2026-01-30

### Fixed
- Timezone inconsistency in notification emails where event start time showed "UTC" but end time showed correct event timezone (e.g., "CET")
- `get_formatted_context_for_template()` now preserves pre-formatted datetime fields instead of overwriting them with UTC-based reformatting

### Changed
- Added `__str__` methods to 26 models missing them (common, questionnaires, events apps)
- Added `select_related()` to admin querysets for `QuarantinedFileAdmin` and `QuestionnaireEvaluationAdmin` to avoid N+1 queries

## [1.31.3] - 2026-01-27

### Added
- `video/webm` MIME type support in questionnaire file upload schema for audio questions

## [1.31.2] - 2026-01-27

### Fixed
- Event questionnaire service edge cases for submission handling
- Admin panel layout improvements in questionnaire and accounts admin

## [1.31.1] - 2026-01-26

### Changed
- Refactored Celery task error handling in `common/tasks.py` for email and Telegram tasks
- Moved file deletion logic out of announcement service into dedicated task

## [1.31.0] - 2026-01-25

### Added
- **Organization Announcements**: New `Announcement` model with targeting options:
  - Target all members, specific membership tiers, staff only, or event attendees
  - Draft and sent status workflow with `sent_at` timestamp
  - `past_visibility` flag for controlling visibility to new members
  - `recipient_count` tracking
- Announcement service with `send_announcement()` and recipient calculation
- Organization admin endpoints: `POST/GET/PUT/DELETE /announcements`
- Event-specific announcements via `event` FK on Announcement model
- Announcement notification templates for email, Telegram, and in-app

## [1.30.2] - 2026-01-23

### Fixed
- N+1 queries in `EligibilityService` by adding proper prefetch for organization members
- RSVP notification now excludes the user who performed the action (self-notification prevention)
- Added `timezone` field to `City` model via migration `0004_add_city_timezone.py`

### Changed
- Refactored benchmark commands into modular structure under `management/commands/benchmark/`
- `user_preferences_service.py`: Added location caching, improved city change handling

## [1.30.1] - 2026-01-23

### Added
- Performance testing framework with Locust in `tests/performance/`
- `bootstrap_perf_tests` management command for setting up performance test data
- `common/middleware/testing.py` with `TestingMiddleware` for performance tests
- `common/testing.py` utilities for performance test client

### Fixed
- N+1 queries in eligibility service prefetch operations

## [1.30.0] - 2026-01-22

### Added
- Silk debug skill (`.claude/skills/silk-debug/SKILL.md`) for profiling analysis
- Notification dispatcher service in `notifications/service/dispatcher.py` for bulk operations
- Expanded seeder with modular components under `management/commands/seeder/`

### Changed
- Rewrote `seed.py` command with improved data generation
- Optimized dashboard and my_status query performance

## [1.28.0] - 2026-01-21

### Added
- `POST /tickets/{id}/unconfirm` endpoint to reverse confirmed tickets back to pending
- `price_paid` field on `Ticket` model for tracking PWYC offline payment amounts
- `EventQuestionnaireSubmission` renamed from generic questionnaire submission for clarity

### Fixed
- Race condition in checkout ticket count using atomic database operations
- Capacity checking reworked with proper locking in `batch_ticket_service.py`

### Changed
- `TicketService.checkout()` now returns accurate ticket counts
- Venue capacity can now be `None` (unlimited) via migration `0044`

## [1.27.0] - 2026-01-21

### Changed
- Minor linting refactors across codebase
- `FollowService` optimizations for query efficiency

## [1.26.0] - 2026-01-20

### Added
- `GET /events/{id}/pronoun-distribution` endpoint returning attendee pronoun statistics
- `pronouns.py` service module with `get_pronoun_distribution()` function
- Admin panel version display in dashboard template
- Profile picture thumbnails in user admin

### Changed
- Split `events.py` controller into `event_public/` module with separate files:
  - `attendance.py`, `details.py`, `discovery.py`, `guest.py`, `tickets.py`

## [1.25.4] - 2026-01-19

### Fixed
- User model refresh from database after profile picture upload in `accounts/controllers/account.py`

## [1.25.3] - 2026-01-19

### Fixed
- File field refresh from database in `common/utils.py` after thumbnail generation

## [1.25.2] - 2026-01-19

### Fixed
- Questionnaire submission response schema in `questionnaires/schema.py`
- Moved submission transform logic from controller to schema layer

## [1.25.1] - 2026-01-18

### Fixed
- libmagic crash in Docker by adding `libmagic1` to Dockerfile

## [1.25.0] - 2026-01-18

### Added
- **Following System**: Users can follow organizations and event series
  - `OrganizationFollow` and `EventSeriesFollow` models in `events/models/follow.py`
  - Notification preferences per follow (`notify_new_events`, `notify_announcements`)
  - `is_archived` soft-delete flag for unfollow without losing history
  - `FollowService` with follow/unfollow/list operations
  - `GET/POST /organizations/{slug}/follow`, `GET/POST /series/{slug}/follow` endpoints
  - Notifications for new events from followed orgs/series
- **User Profile Enhancements**:
  - `bio` text field and `profile_picture` image field on `RevelUser`
  - `profile_picture_preview` and `profile_picture_thumbnail` generated derivatives
  - `PUT /account/profile-picture` endpoint for upload
- **Thumbnail Generation System**:
  - `common/thumbnails/` module with `ThumbnailService` and configuration
  - `generate_thumbnails` management command for batch processing
  - Automatic thumbnail generation on file upload via signals
- **File Upload Questions**:
  - `FileUploadQuestion` model in questionnaires
  - `QuestionnaireFile` model for storing uploaded files
  - Audio, video, document, and image file type support with MIME validation
  - File upload controller endpoints
- **Signed URLs**:
  - `common/signing.py` with URL signing for protected file access
  - `SignedURLConfig` for configurable expiry and validation
- **Event Gate**: `requires_full_profile` flag on Event model

### Changed
- Protected file fields now use `ProtectedFileField` from `common/fields.py`
- Added `docs/PROTECTED_FILES_CADDY.md` documentation

## [1.23.0] - 2026-01-15

### Added
- **Post-Event Feedback Questionnaires**:
  - `FeedbackService` in `events/service/feedback_service.py`
  - Event-linked feedback questionnaire assignments
  - Automatic feedback request notifications after event ends
- **Admin Impersonation**:
  - `ImpersonationLog` model in `accounts/models.py` for audit trail
  - `ImpersonationService` with `start_impersonation()` and `end_impersonation()`
  - Admin action to impersonate users with confirmation dialog
  - `impersonate_confirm.html` template in admin
  - JWT tokens include impersonation metadata

## [1.22.0] - 2026-01-15

### Fixed
- `depends_on_option_id` resolution in questionnaire question and section creation
- Questionnaire service now properly validates conditional dependencies
- Improved questionnaire admin interface with better option handling

## [1.21.2] - 2026-01-14

### Fixed
- Email greeting changed from `user.first_name` to `user.display_name` across all templates
- Address visibility in notification context for events
- Membership granted notification templates improved with organization link

## [1.21.1] - 2026-01-14

### Added
- Mailpit integration for local email testing (replaces MailHog)
- `compose.yaml` for local development with Mailpit
- Dashboard calendar endpoint tests in `test_dashboard_calendar.py`

### Changed
- Split `events/admin.py` into separate modules under `events/admin/`:
  - `base.py`, `blacklist.py`, `event.py`, `organization.py`, `preferences.py`, `ticket.py`, `venue.py`
- Renamed `docker-compose-dev.yml` to `docker-compose-ci.yml`

## [1.21.0] - 2026-01-14

### Changed
- Major dependency upgrades in `pyproject.toml` and `uv.lock`
- Filter improvements in `events/filters.py`

## [1.20.4] - 2026-01-14

### Changed
- **Major test file refactoring** for maintainability:
  - Split `test_event_admin_controller.py` (2813 lines) into focused test modules
  - Split `test_event_controller.py` (1522 lines) into `test_event_controller/` package
  - Split `test_organization_admin_controller.py` (2492 lines) into modules
  - Split `test_questionnaire_controller.py` (2305 lines) into modules
  - Split `test_batch_ticket_service.py`, `test_event_manager.py`, `test_stripe_service.py`
- **Bootstrap command refactoring**:
  - Split `bootstrap_events.py` (2249 lines) into `bootstrap_helpers/` modules
- **Model file splitting**:
  - Split `event.py` model into `invitation.py`, `potluck.py`, `rsvp.py`, `ticket.py`
- Added file length check to CI via `scripts/check-file-length.sh`

## [1.20.3] - 2026-01-13

### Changed
- Split `events/schema.py` (2026 lines) into `events/schema/` package with modules:
  - `blacklist.py`, `dietary.py`, `event.py`, `invitation.py`, `misc.py`, `organization.py`, etc.
- Split `stripe_service.py` into `stripe_service.py` (checkout) and `stripe_webhooks.py`
- Split `event_service.py` into focused modules including `calendar_utils.py`, `dietary.py`, `duplication.py`
- Split `event_manager.py` into `event_manager/` package with `gates.py`, `manager.py`, `service.py`

## [1.20.2] - 2026-01-13

### Fixed
- Questionnaire notification signals wrapped in `transaction.on_commit()` to prevent race conditions
- Automatic questionnaire evaluation now only triggers for `AUTO` or `HYBRID` evaluation modes

## [1.20.0] - 2026-01-13

### Added
- **Organization Blacklist System**:
  - `Blacklist` model with email and optional fuzzy name matching
  - `BlacklistService` with `add_to_blacklist()`, `remove_from_blacklist()`, `check_blacklisted()`
  - Admin endpoints: `GET/POST/DELETE /organizations/{slug}/blacklist`
  - Whitelist request workflow for blacklisted users to request access
  - Blacklist notifications (approval, rejection, creation)
  - Telegram bot callbacks for whitelist request actions

## [1.19.0] - 2026-01-12

### Added
- Location map URLs: `google_maps_url` and `apple_maps_url` fields on Event
- `address_visibility_message` field for custom address visibility text
- `GET /organizations/{slug}/members` endpoint for admins to list members
- Membership info (`member_since`, `tier`) added to RSVP and Ticket schemas

## [1.18.1] - 2026-01-11

### Fixed
- `event.start` used as fallback when `event.apply_by` is not set in eligibility checks

## [1.18.0] - 2026-01-10

### Added
- `apply_by` deadline field on Event model for invitation requests and questionnaire submissions
- Application deadlines now separate from event start time
- Eligibility gate checks `apply_by` before `start`

## [1.17.2] - 2026-01-10

### Fixed
- Questionnaire notification improvements with permission-based filtering
- Updated German (`de`) and Italian (`it`) translation files

## [1.17.1] - 2026-01-09

### Fixed
- Bootstrap data for conditional questionnaires in `bootstrap_events.py`

## [1.17.0] - 2026-01-09

### Added
- **Conditional Questions and Sections**:
  - `depends_on_option` FK on `Question` and `Section` models
  - Questions/sections only shown when specified option is selected
  - `WAIT_FOR_INVITATION_APPROVAL` step in eligibility flow for pending invitation requests

## [1.16.3] - 2026-01-07

### Fixed
- Tasks now triggered manually after batch ticket creation (signals weren't firing for bulk_create)
- `last_login` timestamp updated when JWT tokens are generated

## [1.16.1] - 2026-01-07

### Fixed
- Added `manual_payment_instructions` to ticket tier create/edit schema

## [1.16.0] - 2026-01-06

### Added
- `max_submission_age` field on Questionnaire for time-limited submissions
- Questionnaire ownership validation improvements

### Changed
- Default LLM backend changed from `OPENAI` to `SANITIZING` for safety

## [1.15.0] - 2026-01-05

### Added
- Markdown support in questionnaire `question_text` and `section_description` fields
- Improved questionnaire admin interface with markdown preview

### Changed
- Questionnaire notification templates updated with clearer formatting

## [1.14.0] - 2025-12-28

### Added
- `members_exempt` boolean flag on organization questionnaires
- `resanitize_markdown` management command for re-processing existing markdown content
- `{% markdown %}` template tag for rendering markdown in templates

### Changed
- **Security**: Replaced `bleach` with `nh3` library for HTML sanitization
- Removed deprecated `_html` suffix attributes from models (now computed dynamically)
- Refactored Makefile to use `uv` instead of pip

## [1.13.5] - 2025-12-19

### Fixed
- Email addresses now lowercased at registration in `account_service.py`

## [1.13.4] - 2025-12-16

### Fixed
- Event token creation with invitation payload properly serialized

## [1.13.3] - 2025-12-12

### Changed
- Version bump only (no code changes)

## [1.13.2] - 2025-12-12

### Added
- Batch ticket support for guest checkout flow
- Signal handlers for ticket creation improved

## [1.13.1] - 2025-12-12

### Added
- Social fields on Organization model:
  - `instagram_handle`, `twitter_handle`, `linkedin_url`, `facebook_url`, `bluesky_handle`, `website_url`

## [1.13.0] - 2025-12-12

### Added
- **Venue and Seating System**:
  - `Venue` model with organization FK, location (PostGIS Point), capacity, address
  - `VenueSector` model for logical areas (e.g., "Balcony", "Floor") with polygon shapes
  - `VenueSeat` model with label, row/number, position coordinates, accessibility flags
  - `venue`, `sector`, `seat` FKs added to `Ticket` and `TicketTier` models
  - `seat_assignment_mode` enum on TicketTier: `NONE`, `RANDOM`, `USER_CHOICE`
  - Unique constraint: one seat per event (prevents double-booking)
- **Batch Ticket Purchase**:
  - `BatchTicketService` for multi-ticket checkout with seat selection
  - `PaymentBatchSession` model for tracking batch payments
  - Guest name support via `guest_name` field on Ticket
- **Venue Admin**:
  - `VenueAdmin`, `VenueSectorAdmin`, `VenueSeatAdmin` in admin panel
  - Inline sectors and seats in venue admin
- **Seating Validation**:
  - Point-in-polygon validation for seat positions within sector shapes
  - Seat deletion blocked if active tickets exist for future events

### Changed
- Test fixtures now use `LocMemCache` instead of Redis flush for parallel test support
- `MD5PasswordHasher` used in tests for faster user creation (~100x speedup)

## [1.12.1] - 2025-12-02

### Fixed
- Email 'to' field always used (never cc/bcc) in notification dispatcher
- Admin actions added for resending verification and password reset emails

## [1.12.0] - 2025-12-01

### Added
- `PUT /account/language` endpoint for updating user language preference independently

## [1.11.0] - 2025-12-01

### Added
- `attendees_only` visibility option for event resources
- `address_visibility` field on Event: `public`, `attendees_only`, or `hidden`

## [1.10.0] - 2025-11-30

### Added
- **Apple Wallet Pass Generation**:
  - `wallet/` app with `ApplePassGenerator` class
  - `apple_pass_available` property on Ticket model (checks config)
  - `TicketWalletController` with `GET /tickets/{id}/wallet/apple` endpoint
  - pkpass attachments automatically included in ticket creation emails
  - Settings: `APPLE_WALLET_PASS_TYPE_ID`, `APPLE_WALLET_TEAM_ID`, `APPLE_WALLET_CERT_PATH`, etc.
- `TicketQuerySet` and `TicketManager` with `with_event()`, `with_tier()`, `with_user()`, `full()` methods

## [1.9.0] - 2025-11-29

### Added
- `POST /events/{id}/duplicate` endpoint for event duplication
- `PUT /events/{id}/slug` endpoint for modifying event slug
- Removed unique constraint on organization-event name combination

### Fixed
- Ambiguous `past_event` parameter behavior in dashboard filters

## [1.8.1] - 2025-11-28

### Fixed
- Metrics (`/metrics`) and healthcheck (`/health`) endpoints exempted from HTTPS redirect middleware

## [1.8.0] - 2025-11-28

### Added
- **Calendar Feed Endpoints**:
  - `GET /calendar/ics` for user's complete event calendar (iCal format)
  - `GET /events/{id}/calendar.ics` for single event calendar
  - `Event.ics()` method for generating iCal content
- **Event Cancellation**:
  - `cancelled` status on Event (separate from soft delete)
  - `DELETE /events/{id}/hard` endpoint for permanent deletion (admin only)

## [1.7.1] - 2025-11-27

### Changed
- Default Stripe commission lowered to 1.5% + 0.25 EUR (from 2% + 0.30 EUR)

## [1.7.0] - 2025-11-26

### Added
- `stripe_account_email` required field on Organization for Stripe Connect setup
- Migration to populate existing Stripe account emails from Stripe API

## [1.6.1] - 2025-11-26

### Added
- **Organization Creation Flow**:
  - Contact email verification workflow for new organizations
  - `contact_email_verified` boolean field on Organization
  - `POST /organizations/{slug}/verify-contact-email` endpoint
  - Verification email sent on organization creation

### Removed
- Unused `show_me_on_attendee_list` field from notification preferences

## [1.5.3] - 2025-11-24

### Fixed
- Verification reminders not being sent due to incorrect model field reference

## [1.5.2] - 2025-11-23

### Fixed
- Ticket HTML template improvements for better rendering
- Notification attachments for ticket creation emails

### Changed
- Potluck notification templates updated for clarity

## [1.5.1] - 2025-11-22

### Fixed
- Stripe commission configuration: platform host no longer receives commission by default

## [1.5.0] - 2025-11-22

### Added
- **Email Verification Reminder System**:
  - `VerificationReminder` model tracking sent reminders
  - `send_verification_reminders` Celery task
  - Automated reminders at 3, 7, and 13 days after registration
  - Final warning at 14 days with automatic account deactivation
  - `VERIFICATION_REMINDER_*` notification types

## [1.4.5] - 2025-11-22

### Fixed
- GDPR data export edge cases for users with complex related data

## [1.4.4] - 2025-11-22

### Fixed
- GDPR data export now properly includes all related models

## [1.4.3] - 2025-11-21

### Fixed
- Token schema fixes for event tokens in `EventTokenSchema`
- Minor event service improvements

## [1.4.2] - 2025-11-21

### Fixed
- `POTLUCK_DELETED` notification added to in-app only notifications list

## [1.4.1] - 2025-11-21

### Fixed
- Default values in notification context for missing fields
- Event open HTML template rendering fixes

## [1.4.0] - 2025-11-20

### Added
- Pushover notification on new user registration for admin alerts
- `PUSHOVER_USER_KEY` and `PUSHOVER_API_TOKEN` settings

## [1.3.0] - 2025-11-20

### Added
- **Django Unfold Admin Dashboard**:
  - Custom admin index page with statistics (users, events, organizations)
  - Dietary models admin: `FoodItemAdmin`, `DietaryRestrictionAdmin`, `DietaryPreferenceAdmin`, `UserDietaryPreferenceAdmin`
  - Inline dietary restrictions and preferences in user admin
  - Color-coded restriction severity display

### Changed
- `requires_ticket` field moved from `EventEditSchema` to `EventCreateSchema` only

## [1.2.0] - 2025-11-20

### Fixed
- Added missing `requires_ticket` field to event create schema (was commented out)

## [1.1.1] - 2025-11-20

### Fixed
- Password validation now accepts `[`, `]`, and `=` as valid special characters
- All notification templates changed from `user.first_name` to `user.display_name`
- Frontend URLs changed from `/organizations/{id}` to `/org/{slug}` in notification links

## [1.1.0] - 2025-11-20

### Added
- `UserRegistrationThrottle` (100/day) for registration and verify-resend endpoints
- `ResendVerificationEmailSchema` for accepting email in request body

### Fixed
- **Security**: `/verify-resend` endpoint no longer requires authentication (prevents user enumeration)
- `/verify-resend` always returns 200 OK regardless of whether email exists (prevents enumeration)
- GDPR data export now handles PostGIS `Point` fields with `GDPRJSONEncoder` serializing to GeoJSON
- All datetime fields in API schemas now use `AwareDatetime` instead of naive `datetime`

### Removed
- `free_for_members` and `free_for_staff` fields from Event model (deprecated, use ticket tiers)
- `/toc` and `/privacy` bot commands (replaced with direct URLs)

### Changed
- EventSeries now has default ordering by `organization__name`, `name`
- Telegram eligibility errors now show appropriate messages and waitlist keyboard

## [1.0.1] - 2025-11-20

### Fixed
- User admin panel: removed `display_name_display` from fieldsets
- GeneralUserPreferencesInline: removed deprecated notification preference fields, kept only `city`

## [1.0.0] - 2025-11-19

Initial release of the Revel Backend platform.

### Core Platform
- Django 5.2+ with Django Ninja API framework
- PostgreSQL with PostGIS for geographic features
- Celery with Redis for background task processing
- JWT authentication with refresh tokens
- Google SSO integration
- Two-factor authentication (TOTP)
- GDPR compliance (data export, account deletion)

### Organization Management
- Organization CRUD with role-based permissions (Owner, Staff, Member)
- Membership tiers with access levels
- Token-based invitations and membership requests
- Stripe Connect integration for payments
- Organization resources (documents, links, files)

### Event Management
- Event CRUD with visibility controls (Public, Members-Only, Invite-Only)
- Event series for recurring events
- Event tokens for shareable links
- Guest access without login
- Waitlist management
- Dietary summary for meal planning

### Ticketing System
- Multiple ticket tiers per event
- Pricing: Fixed, Free, Pay-What-You-Can
- Payment methods: Online (Stripe), Offline, At-door, Free
- QR code check-in

### RSVP System
- Yes/No/Maybe responses with deadlines
- Guest RSVP with email confirmation

### Invitation System
- Direct email invitations
- Pending invitations for unregistered users
- Token-based invitation links
- Invitation requests for private events

### Questionnaire System
- Dynamic questionnaires with sections and questions
- Multiple choice and free text questions
- Automatic scoring with thresholds
- LLM-powered evaluation for free text
- Manual review workflow

### Potluck Coordination
- Item management with quantities
- Claiming and status tracking

### Notifications
- Multi-channel: Email, In-app, Telegram
- Granular notification preferences
- One-click email unsubscribe

### Geolocation
- World cities database
- IP-based location detection
- Distance calculations

### Observability
- Structured JSON logging
- OpenTelemetry tracing
- Prometheus metrics
- Pyroscope profiling

### Security
- ClamAV malware scanning
- File quarantine system
- Rate limiting

### Internationalization
- English, German, Italian support

[Unreleased]: https://github.com/letsrevel/revel-backend/compare/v1.47.0...HEAD
[1.47.0]: https://github.com/letsrevel/revel-backend/compare/v1.46.0...v1.47.0
[1.46.0]: https://github.com/letsrevel/revel-backend/compare/v1.45.1...v1.46.0
[1.45.1]: https://github.com/letsrevel/revel-backend/compare/v1.45.0...v1.45.1
[1.45.0]: https://github.com/letsrevel/revel-backend/compare/v1.44.1...v1.45.0
[1.44.1]: https://github.com/letsrevel/revel-backend/compare/v1.44.0...v1.44.1
[1.44.0]: https://github.com/letsrevel/revel-backend/compare/v1.43.0...v1.44.0
[1.43.0]: https://github.com/letsrevel/revel-backend/compare/v1.42.3...v1.43.0
[1.42.3]: https://github.com/letsrevel/revel-backend/compare/v1.42.2...v1.42.3
[1.42.2]: https://github.com/letsrevel/revel-backend/compare/v1.42.1...v1.42.2
[1.42.1]: https://github.com/letsrevel/revel-backend/compare/v1.42.0...v1.42.1
[1.42.0]: https://github.com/letsrevel/revel-backend/compare/v1.41.2...v1.42.0
[1.41.2]: https://github.com/letsrevel/revel-backend/compare/v1.41.1...v1.41.2
[1.41.1]: https://github.com/letsrevel/revel-backend/compare/v1.41.0...v1.41.1
[1.41.0]: https://github.com/letsrevel/revel-backend/compare/v1.40.0...v1.41.0
[1.40.0]: https://github.com/letsrevel/revel-backend/compare/v1.39.0...v1.40.0
[1.39.0]: https://github.com/letsrevel/revel-backend/compare/v1.38.5...v1.39.0
[1.38.5]: https://github.com/letsrevel/revel-backend/compare/v1.38.4...v1.38.5
[1.38.4]: https://github.com/letsrevel/revel-backend/compare/v1.38.3...v1.38.4
[1.38.3]: https://github.com/letsrevel/revel-backend/compare/v1.38.2...v1.38.3
[1.38.2]: https://github.com/letsrevel/revel-backend/compare/v1.38.1...v1.38.2
[1.38.1]: https://github.com/letsrevel/revel-backend/compare/v1.38.0...v1.38.1
[1.38.0]: https://github.com/letsrevel/revel-backend/compare/v1.37.3...v1.38.0
[1.37.3]: https://github.com/letsrevel/revel-backend/compare/v1.37.2...v1.37.3
[1.37.2]: https://github.com/letsrevel/revel-backend/compare/v1.37.1...v1.37.2
[1.37.1]: https://github.com/letsrevel/revel-backend/compare/v1.37.0...v1.37.1
[1.37.0]: https://github.com/letsrevel/revel-backend/compare/v1.36.3...v1.37.0
[1.36.3]: https://github.com/letsrevel/revel-backend/compare/v1.36.0...v1.36.3
[1.36.0]: https://github.com/letsrevel/revel-backend/compare/v1.35.1...v1.36.0
[1.35.1]: https://github.com/letsrevel/revel-backend/compare/v1.35.0...v1.35.1
[1.35.0]: https://github.com/letsrevel/revel-backend/compare/v1.34.4...v1.35.0
[1.34.4]: https://github.com/letsrevel/revel-backend/compare/v1.34.3...v1.34.4
[1.34.3]: https://github.com/letsrevel/revel-backend/compare/v1.34.2...v1.34.3
[1.34.2]: https://github.com/letsrevel/revel-backend/compare/v1.34.1...v1.34.2
[1.34.1]: https://github.com/letsrevel/revel-backend/compare/v1.34.0...v1.34.1
[1.34.0]: https://github.com/letsrevel/revel-backend/compare/v1.33.0...v1.34.0
[1.33.0]: https://github.com/letsrevel/revel-backend/compare/v1.32.0...v1.33.0
[1.32.0]: https://github.com/letsrevel/revel-backend/compare/v1.31.13...v1.32.0
[1.31.13]: https://github.com/letsrevel/revel-backend/compare/v1.31.12...v1.31.13
[1.31.12]: https://github.com/letsrevel/revel-backend/compare/v1.31.11...v1.31.12
[1.31.11]: https://github.com/letsrevel/revel-backend/compare/v1.31.10...v1.31.11
[1.31.10]: https://github.com/letsrevel/revel-backend/compare/v1.31.9...v1.31.10
[1.31.9]: https://github.com/letsrevel/revel-backend/compare/v1.31.8...v1.31.9
[1.31.8]: https://github.com/letsrevel/revel-backend/compare/v1.31.7...v1.31.8
[1.31.7]: https://github.com/letsrevel/revel-backend/compare/v1.31.5...v1.31.7
[1.31.5]: https://github.com/letsrevel/revel-backend/compare/v1.31.4...v1.31.5
[1.31.4]: https://github.com/letsrevel/revel-backend/compare/v1.31.3...v1.31.4
[1.31.3]: https://github.com/letsrevel/revel-backend/compare/v1.31.2...v1.31.3
[1.31.2]: https://github.com/letsrevel/revel-backend/compare/v1.31.1...v1.31.2
[1.31.1]: https://github.com/letsrevel/revel-backend/compare/v1.31.0...v1.31.1
[1.31.0]: https://github.com/letsrevel/revel-backend/compare/v1.30.2...v1.31.0
[1.30.2]: https://github.com/letsrevel/revel-backend/compare/v1.30.1...v1.30.2
[1.30.1]: https://github.com/letsrevel/revel-backend/compare/v1.30.0...v1.30.1
[1.30.0]: https://github.com/letsrevel/revel-backend/compare/v1.28.0...v1.30.0
[1.28.0]: https://github.com/letsrevel/revel-backend/compare/v1.27.0...v1.28.0
[1.27.0]: https://github.com/letsrevel/revel-backend/compare/v1.26.0...v1.27.0
[1.26.0]: https://github.com/letsrevel/revel-backend/compare/v1.25.4...v1.26.0
[1.25.4]: https://github.com/letsrevel/revel-backend/compare/v1.25.3...v1.25.4
[1.25.3]: https://github.com/letsrevel/revel-backend/compare/v1.25.2...v1.25.3
[1.25.2]: https://github.com/letsrevel/revel-backend/compare/v1.25.1...v1.25.2
[1.25.1]: https://github.com/letsrevel/revel-backend/compare/v1.25.0...v1.25.1
[1.25.0]: https://github.com/letsrevel/revel-backend/compare/v1.23.0...v1.25.0
[1.23.0]: https://github.com/letsrevel/revel-backend/compare/v1.22.0...v1.23.0
[1.22.0]: https://github.com/letsrevel/revel-backend/compare/v1.21.2...v1.22.0
[1.21.2]: https://github.com/letsrevel/revel-backend/compare/v1.21.1...v1.21.2
[1.21.1]: https://github.com/letsrevel/revel-backend/compare/v1.21.0...v1.21.1
[1.21.0]: https://github.com/letsrevel/revel-backend/compare/v1.20.4...v1.21.0
[1.20.4]: https://github.com/letsrevel/revel-backend/compare/v1.20.3...v1.20.4
[1.20.3]: https://github.com/letsrevel/revel-backend/compare/v1.20.2...v1.20.3
[1.20.2]: https://github.com/letsrevel/revel-backend/compare/v1.20.0...v1.20.2
[1.20.0]: https://github.com/letsrevel/revel-backend/compare/v1.19.0...v1.20.0
[1.19.0]: https://github.com/letsrevel/revel-backend/compare/v1.18.1...v1.19.0
[1.18.1]: https://github.com/letsrevel/revel-backend/compare/v1.18.0...v1.18.1
[1.18.0]: https://github.com/letsrevel/revel-backend/compare/v1.17.2...v1.18.0
[1.17.2]: https://github.com/letsrevel/revel-backend/compare/v1.17.1...v1.17.2
[1.17.1]: https://github.com/letsrevel/revel-backend/compare/v1.17.0...v1.17.1
[1.17.0]: https://github.com/letsrevel/revel-backend/compare/v1.16.3...v1.17.0
[1.16.3]: https://github.com/letsrevel/revel-backend/compare/v1.16.1...v1.16.3
[1.16.1]: https://github.com/letsrevel/revel-backend/compare/v1.16.0...v1.16.1
[1.16.0]: https://github.com/letsrevel/revel-backend/compare/v1.15.0...v1.16.0
[1.15.0]: https://github.com/letsrevel/revel-backend/compare/v1.14.0...v1.15.0
[1.14.0]: https://github.com/letsrevel/revel-backend/compare/v1.13.5...v1.14.0
[1.13.5]: https://github.com/letsrevel/revel-backend/compare/v1.13.4...v1.13.5
[1.13.4]: https://github.com/letsrevel/revel-backend/compare/v1.13.3...v1.13.4
[1.13.3]: https://github.com/letsrevel/revel-backend/compare/v1.13.2...v1.13.3
[1.13.2]: https://github.com/letsrevel/revel-backend/compare/v1.13.1...v1.13.2
[1.13.1]: https://github.com/letsrevel/revel-backend/compare/v1.13.0...v1.13.1
[1.13.0]: https://github.com/letsrevel/revel-backend/compare/v1.12.1...v1.13.0
[1.12.1]: https://github.com/letsrevel/revel-backend/compare/v1.12.0...v1.12.1
[1.12.0]: https://github.com/letsrevel/revel-backend/compare/v1.11.0...v1.12.0
[1.11.0]: https://github.com/letsrevel/revel-backend/compare/v1.10.0...v1.11.0
[1.10.0]: https://github.com/letsrevel/revel-backend/compare/v1.9.0...v1.10.0
[1.9.0]: https://github.com/letsrevel/revel-backend/compare/v1.8.1...v1.9.0
[1.8.1]: https://github.com/letsrevel/revel-backend/compare/v1.8.0...v1.8.1
[1.8.0]: https://github.com/letsrevel/revel-backend/compare/v1.7.1...v1.8.0
[1.7.1]: https://github.com/letsrevel/revel-backend/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/letsrevel/revel-backend/compare/v1.6.1...v1.7.0
[1.6.1]: https://github.com/letsrevel/revel-backend/compare/v1.5.3...v1.6.1
[1.5.3]: https://github.com/letsrevel/revel-backend/compare/v1.5.2...v1.5.3
[1.5.2]: https://github.com/letsrevel/revel-backend/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/letsrevel/revel-backend/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/letsrevel/revel-backend/compare/v1.4.5...v1.5.0
[1.4.5]: https://github.com/letsrevel/revel-backend/compare/v1.4.4...v1.4.5
[1.4.4]: https://github.com/letsrevel/revel-backend/compare/v1.4.3...v1.4.4
[1.4.3]: https://github.com/letsrevel/revel-backend/compare/v1.4.2...v1.4.3
[1.4.2]: https://github.com/letsrevel/revel-backend/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/letsrevel/revel-backend/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/letsrevel/revel-backend/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/letsrevel/revel-backend/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/letsrevel/revel-backend/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/letsrevel/revel-backend/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/letsrevel/revel-backend/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/letsrevel/revel-backend/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/letsrevel/revel-backend/releases/tag/v1.0.0
