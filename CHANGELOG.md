# Changelog

All notable changes to the Revel Backend are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Discord admin-ping channel (`DISCORD_ADMIN_WEBHOOK_URL`) for new-user and new-organization events. The new-user Discord ping is PII-free (count only, skipped for guest signups); the new-organization Discord ping includes org name + owner email
- `SiteSettings.notify_organization_created` flag to gate new-organization admin pings
- New-user Pushover ping now includes the referrer's email (when present) and a running user count; new-organization Pushover ping includes owner email + running org count

### Changed
- Decomposed long functions into focused helpers for readability
- Extracted `is_owner_or_staff` and `has_org_permission` permission helpers

### Fixed
- Unbanning a user now clears the stale `BANNED` `OrganizationMember` row that the ban created. The membership transitions to `CANCELLED` (not `ACTIVE`, to avoid implicitly re-granting prior membership privileges), so the org and its events become visible again. If another blacklist entry still covers the user in the org, the `BANNED` status is preserved.

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
