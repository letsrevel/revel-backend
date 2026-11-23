# Gauntlet regions: revel-backend

<!-- Used by /gauntlet:vuln-scan and /gauntlet:tech-debt full sweeps.
Feature slices across layers (controllers + services + models + schemas + tasks + admin of one feature together).
Every tracked non-test source file under src/ matches exactly one region (tests: any path under a tests/ directory, and conftest.py).
Glob syntax: `**` crosses directories, `*` stays within one path segment. Paths are repo-relative.
Line counts below are non-test, non-migration Python lines; keep each region under ~15k. -->

## events-checkout-payments
- globs: `src/events/service/batch_ticket_service/**`, `src/events/service/event_manager/**`, `src/events/service/guest.py`, `src/events/service/guest_hold_session.py`, `src/events/service/pending_checkout.py`, `src/events/service/stripe_service.py`, `src/events/service/stripe_webhooks.py`, `src/events/service/stripe_webhook_disputes.py`, `src/events/service/stripe_address_service.py`, `src/events/service/stripe_incidents.py`, `src/events/service/payment_means_service.py`, `src/events/service/discount_code_service.py`, `src/events/service/ticket_price.py`, `src/events/service/billing_profile.py`, `src/events/service/tier_eligibility.py`, `src/events/service/event_questionnaire_service.py`, `src/events/controllers/event_public/tickets.py`, `src/events/controllers/event_public/guest.py`, `src/events/controllers/stripe_webhook.py`, `src/events/controllers/organization_admin/discount_codes.py`, `src/events/models/payment.py`, `src/events/models/discount_code.py`, `src/events/models/dispute.py`, `src/events/models/stripe_webhook_event.py`, `src/events/schema/checkout.py`, `src/events/schema/guest_checkout.py`, `src/events/schema/discount_code.py`, `src/events/tasks/payments.py`, `src/events/tasks/stripe_webhooks.py`, `src/events/admin/discount_code.py`, `src/events/admin/dispute.py`, `src/events/admin/stripe_webhook_event.py`, `src/events/utils/tier_pricing.py`, `src/events/utils/purchase_rules.py`, `src/events/utils/billing_address.py`, `src/events/utils/currency.py`, `src/events/management/commands/provision_stripe_webhooks.py`, `src/events/management/commands/scrub_stripe_products.py`, `src/events/management/commands/resync_subscription_fees.py`
- context: `src/events/utils/__init__.py`, `src/events/utils/hostname.py`, `src/events/utils/membership_access.py`, `src/events/models/ticket.py`, `src/events/service/ticket_service.py`, `src/common/service/stripe_config.py`, `src/common/service/stripe_connect_service.py`, `../infra/Caddyfile`
- risk: CRITICAL
- tier: opus
- security focus: BOLA on order, ticket and payment ids; guest checkout (anonymous, email-claimed, tenant-aware links); price and discount tampering (client-supplied overrides, uniform cart price assumption); reserve/session split and `select_for_update` ordering; Stripe webhook signature trust, idempotency and replay; eligibility gates (visibility, questionnaires, invitations, tier restrictions) enforced on every purchase path; oversell races.
- debt focus: size and branching of checkout/webhook modules; duplicated pricing and eligibility logic between batch, guest and legacy paths; dead legacy checkout code; mock-heavy tests.

## events-tickets-seating
- globs: `src/events/service/ticket_service.py`, `src/events/service/ticket_file_service.py`, `src/events/service/ticket_number_service.py`, `src/events/service/ticket_guest_name_service.py`, `src/events/service/check_in_service.py`, `src/events/service/cancellation_service.py`, `src/events/service/potluck_service.py`, `src/events/service/seating/**`, `src/events/controllers/event_public/seating.py`, `src/events/controllers/event_public/attendance.py`, `src/events/controllers/event_admin/tickets.py`, `src/events/controllers/event_admin/seating.py`, `src/events/controllers/event_admin/rsvps.py`, `src/events/controllers/organization_admin/tickets.py`, `src/events/controllers/potluck.py`, `src/events/models/ticket.py`, `src/events/models/ticket_queryset.py`, `src/events/models/ticket_number.py`, `src/events/models/seating.py`, `src/events/models/rsvp.py`, `src/events/models/potluck.py`, `src/events/schema/ticket.py`, `src/events/schema/ticket_detail.py`, `src/events/schema/ticket_tier.py`, `src/events/schema/ticket_cancellation.py`, `src/events/schema/seating.py`, `src/events/schema/rsvp.py`, `src/events/schema/potluck.py`, `src/events/tasks/attendees.py`, `src/events/tasks/seating.py`, `src/events/admin/ticket.py`, `src/events/service/venue_service.py`, `src/events/controllers/organization_admin/venues.py`, `src/events/models/venue.py`, `src/events/schema/venue.py`, `src/events/admin/venue.py`
- context: `src/events/utils/__init__.py`, `src/events/service/event_manager/gates.py`, `src/events/service/batch_ticket_service/seats.py`, `src/events/models/event.py`
- risk: CRITICAL
- tier: opus
- security focus: attendance definition (ACTIVE + CHECKED_IN only; PENDING never unlocks attendee-only data); QR/check-in token handling and replay; seat lock protocol and hold takeover; BOLA on ticket ids across organizations; cancellation and refund-window enforcement; capacity reclaim (locking, floor); manual payment confirm/unconfirm price invariants; ticket file/PDF access.
- debt focus: ticket_service size; seating pricing/paint-report complexity; unlock-by-status logic scattered instead of `attending()`.

## events-subscriptions
- globs: `src/events/service/subscription/**`, `src/events/service/stripe_webhook_subscriptions.py`, `src/events/controllers/me_subscriptions.py`, `src/events/controllers/organization_admin/subscriptions.py`, `src/events/models/subscription.py`, `src/events/schema/subscription.py`, `src/events/schema/subscription_plan_change.py`, `src/events/schema/subscription_metrics.py`, `src/events/tasks/subscriptions.py`, `src/events/admin/subscription.py`, `src/events/utils/membership_payment_rules.py`, `src/events/utils/subscription_payment.py`, `src/events/utils/subscription_periods.py`, `src/events/utils/subscription_plan_rules.py`
- context: `src/events/service/membership_manager/**`, `src/events/utils/membership_access.py`, `src/common/service/stripe_connect_service.py`
- risk: CRITICAL
- tier: opus
- security focus: Stripe Connect subscription lifecycle (checkout, plan change, uncancel, dunning, terminal states and revival); webhook trust and event ordering; fee computation and drift; offline vs online hybrid payment edits; BOLA on subscription and plan ids; tenant-correct billing.
- debt focus: lifecycle.py, sync.py and checkout.py size; state-machine duplication between webhook, sync and task paths.

## events-memberships-register
- globs: `src/events/service/membership_manager/**`, `src/events/service/member_register_admin_service.py`, `src/events/service/member_register_linking.py`, `src/events/service/member_register_reconciliation.py`, `src/events/service/member_register_service.py`, `src/events/service/day_membership_service.py`, `src/events/service/membership_questionnaire_service.py`, `src/events/service/member_scan_service.py`, `src/events/service/organization_service/membership.py`, `src/events/service/export/member_register_export.py`, `src/events/controllers/me_applications.py`, `src/events/controllers/me_membership_questionnaire.py`, `src/events/controllers/member_register_invites.py`, `src/events/controllers/organization_admin/members.py`, `src/events/controllers/organization_admin/membership_requests.py`, `src/events/controllers/organization_admin/member_register.py`, `src/events/controllers/organization_admin/member_register_linking.py`, `src/events/models/membership.py`, `src/events/models/membership_record.py`, `src/events/schema/member_register.py`, `src/events/schema/application.py`, `src/events/tasks/member_register.py`, `src/events/admin/membership_record.py`, `src/events/utils/membership_access.py`
- context: `src/events/models/organization.py`, `src/events/controllers/permissions.py`
- risk: HIGH
- tier: opus
- security focus: two-layer member register (org-held records vs account-linked memberships); linking and invite flows (claim, merge, reconcile) crossing accounts; membership status transitions and role ceilings; day memberships; member-scan QR; GDPR export of register data; BOLA across organizations.
- debt focus: membership_manager gates and service size; linking/reconciliation duplication; organization membership service (817 lines).

## events-invoicing-vat-refunds
- globs: `src/events/service/invoice_service.py`, `src/events/service/attendee_invoice_draft_service.py`, `src/events/service/attendee_invoice_lines.py`, `src/events/service/attendee_invoice_service.py`, `src/events/service/attendee_credit_note_service.py`, `src/events/service/uncovered_refund_service.py`, `src/events/service/attendee_vat_service.py`, `src/events/service/einvoice_mapping.py`, `src/events/service/einvoice_service.py`, `src/events/service/en16931_totals_service.py`, `src/events/service/credit_note_lines.py`, `src/events/service/credit_note_totals.py`, `src/events/service/vat_service.py`, `src/events/service/vies_service.py`, `src/events/service/zero_rate_guard_service.py`, `src/events/service/small_business_scheme_service.py`, `src/events/service/skipped_fiscal_document_service.py`, `src/events/service/refund_service.py`, `src/events/service/stripe_webhook_refunds.py`, `src/events/service/revenue_aggregation.py`, `src/events/service/revenue_report_service.py`, `src/events/service/platform_revenue_service.py`, `src/events/service/referral_payout_service.py`, `src/events/compliance/**`, `src/events/controllers/organization_admin/vat.py`, `src/events/controllers/organization_admin/revenue.py`, `src/events/models/attendee_invoice.py`, `src/events/models/invoice.py`, `src/events/models/refund.py`, `src/events/models/skipped_fiscal_document.py`, `src/events/schema/invoice.py`, `src/events/schema/financials.py`, `src/events/schema/revenue_report.py`, `src/events/schema/vat_preview.py`, `src/events/tasks/invoicing.py`, `src/events/tasks/refunds.py`, `src/events/tasks/revenue.py`, `src/events/admin/attendee_invoice.py`, `src/events/admin/invoice.py`, `src/events/utils/small_business.py`, `src/events/utils/vat_category.py`, `src/events/utils/refund_policy.py`, `src/events/utils/supply_date.py`, `src/events/management/commands/report_mischarged_attendee_vat.py`, `src/events/management/commands/backfill_org_billing_addresses.py`
- context: `src/common/service/vat_utils.py`, `src/common/service/vies_service.py`, `src/common/service/invoice_utils.py`, `src/common/service/exchange_rate_service.py`, `src/common/templatetags/invoice_filters.py`, `src/events/service/stripe_service.py`, `src/events/models/ticket.py`, `src/events/models/payment.py`, `src/templates/invoices/**`, `docs/architecture/billing-and-vat.md`, `docs/compliance/**`
- risk: CRITICAL
- tier: opus
- security focus: refund ceilings, pro-rata fee apportionment (largest remainder), partial refunds and Stripe-dashboard refunds matched by amount; row locks held across Stripe calls; VAT place-of-supply and reverse-charge decisions; invoice and credit-note numbering gaps and immutability; BOLA on invoice downloads; tenant-correct invoice sender identity; country compliance policy enforcement.
- debt focus: attendee_invoice_service and revenue_aggregation near the file-length cap; country policy duplication; money-rounding helpers duplicated across services.

## events-organization-tenancy
- globs: `src/events/service/organization_service/__init__.py`, `src/events/service/organization_service/contact.py`, `src/events/service/organization_service/lifecycle.py`, `src/events/service/organization_service/tokens.py`, `src/events/service/custom_domain_service.py`, `src/events/service/cloudflare_custom_hostnames.py`, `src/events/service/cloudflare_turnstile.py`, `src/events/service/turnstile_hostname_service.py`, `src/events/service/dns_diagnostics.py`, `src/events/service/custom_domain_notifications.py`, `src/events/service/tenant_service.py`, `src/events/service/org_user_lookup_service.py`, `src/events/service/permission_snapshot.py`, `src/events/service/tokens.py`, `src/events/service/invitation_service.py`, `src/events/service/invitations.py`, `src/events/service/blacklist_service.py`, `src/events/service/whitelist_service.py`, `src/events/service/resource_service.py`, `src/events/controllers/organization.py`, `src/events/controllers/permissions.py`, `src/events/controllers/tls_ask.py`, `src/events/controllers/organization_admin/__init__.py`, `src/events/controllers/organization_admin/base.py`, `src/events/controllers/organization_admin/blacklist.py`, `src/events/controllers/organization_admin/core.py`, `src/events/controllers/organization_admin/custom_domain.py`, `src/events/controllers/organization_admin/resources.py`, `src/events/controllers/organization_admin/tokens.py`, `src/events/controllers/organization_admin/user_lookup.py`, `src/events/controllers/organization_admin/whitelist.py`, `src/events/controllers/event_admin/tokens.py`, `src/events/controllers/event_admin/invitations.py`, `src/events/controllers/event_admin/invitation_requests.py`, `src/events/models/organization.py`, `src/events/models/organization_domain.py`, `src/events/models/blacklist.py`, `src/events/models/invitation.py`, `src/events/models/reserved_slug_token.py`, `src/events/schema/organization.py`, `src/events/schema/custom_domain.py`, `src/events/schema/blacklist.py`, `src/events/schema/invitation.py`, `src/events/schema/org_user_lookup.py`, `src/events/tasks/organization.py`, `src/events/tasks/custom_domains.py`, `src/events/admin/__init__.py`, `src/events/admin/base.py`, `src/events/admin/organization.py`, `src/events/admin/organization_domain.py`, `src/events/admin/blacklist.py`, `src/events/admin/reserved_slug_token.py`, `src/events/constants/**`, `src/events/utils/hostname.py`, `src/events/utils/reserved_slug_tokens.py`, `src/events/utils/blacklist.py`, `src/events/utils/branding.py`, `src/events/utils/brand_theme.py`, `src/events/utils/pass_branding.py`
- context: `../infra/Caddyfile`, `src/common/authentication.py`, `src/oauth/scopes.py`, `src/oauth/permissions.py`, `docs/architecture/permissions.md`, `docs/architecture/white-label.md`
- risk: CRITICAL
- tier: opus
- security focus: Owner/Staff/Member permission checks on every org-scoped controller (object-level, not just class-level); invitation and token claim flows (bans, expiry, usage limits, scopes, role ceilings); custom-domain onboarding (hostname ownership proof, TLS ask endpoint, Turnstile hostname binding, Cloudflare API calls, cross-tenant leakage, no platform tokens on tenant domains); reserved slugs; blacklist/whitelist matching; staff privilege escalation through staff-editable fields.
- debt focus: organization admin and model size; permission-check duplication across controllers; custom_domain_service (811 lines).

## events-org-comms-notes
- globs: `src/events/service/announcement_service.py`, `src/events/service/org_nudge_service.py`, `src/events/service/admin_recap_service.py`, `src/events/service/user_note_service.py`, `src/events/service/user_note_transfer.py`, `src/events/controllers/organization_admin/announcements.py`, `src/events/controllers/organization_admin/user_notes.py`, `src/events/models/announcement.py`, `src/events/models/nudge.py`, `src/events/models/user_note.py`, `src/events/schema/announcement.py`, `src/events/schema/user_note.py`, `src/events/tasks/announcements.py`, `src/events/tasks/admin_recap.py`, `src/events/admin/announcement.py`, `src/events/admin/nudge.py`, `src/events/admin/user_note.py`
- context: `src/notifications/service/**`, `docs/architecture/scheduled-announcements.md`
- risk: MEDIUM
- tier: sonnet
- security focus: announcement audience resolution (who receives, per-organization mute, opt-out honoured); staff-only visibility of user notes and their transfer on account merge; scheduled-send authorization; HTML/markdown sanitization of announcement bodies.
- debt focus: announcement_service size; nudge beat cadence and idempotence.

## events-discovery-visibility
- globs: `src/events/__init__.py`, `src/events/apps.py`, `src/events/checks.py`, `src/events/suppression.py`, `src/events/exceptions.py`, `src/events/exception_handlers.py`, `src/events/filters.py`, `src/events/signals.py`, `src/events/service/__init__.py`, `src/events/service/dashboard_service.py`, `src/events/service/follow_service.py`, `src/events/service/bookmark_service.py`, `src/events/service/calendar_utils.py`, `src/events/service/location_service.py`, `src/events/service/duplication.py`, `src/events/service/dietary.py`, `src/events/service/pronouns.py`, `src/events/service/user_preferences_service.py`, `src/events/service/feedback_service.py`, `src/events/service/export/__init__.py`, `src/events/service/export/attendee_export.py`, `src/events/service/export/formatting.py`, `src/events/service/export/questionnaire_export.py`, `src/events/controllers/__init__.py`, `src/events/controllers/dashboard.py`, `src/events/controllers/following.py`, `src/events/controllers/exports.py`, `src/events/controllers/user_preferences.py`, `src/events/controllers/event_public/__init__.py`, `src/events/controllers/event_public/base.py`, `src/events/controllers/event_public/details.py`, `src/events/controllers/event_public/discovery.py`, `src/events/controllers/event_admin/__init__.py`, `src/events/controllers/event_admin/base.py`, `src/events/controllers/event_admin/core.py`, `src/events/models/__init__.py`, `src/events/models/event.py`, `src/events/models/follow.py`, `src/events/models/bookmark.py`, `src/events/models/misc.py`, `src/events/models/mixins.py`, `src/events/models/preferences.py`, `src/events/models/questionnaire.py`, `src/events/schema/__init__.py`, `src/events/schema/event.py`, `src/events/schema/follow.py`, `src/events/schema/bookmark.py`, `src/events/schema/misc.py`, `src/events/schema/mixins.py`, `src/events/schema/questionnaire.py`, `src/events/schema/preferences.py`, `src/events/schema/pronouns.py`, `src/events/schema/dietary.py`, `src/events/schema/export.py`, `src/events/schema/attribution.py`, `src/events/tasks/__init__.py`, `src/events/tasks/exports.py`, `src/events/admin/event.py`, `src/events/admin/bookmark.py`, `src/events/admin/follow.py`, `src/events/admin/preferences.py`, `src/events/admin/questionnaire.py`, `src/events/utils/__init__.py`, `src/events/utils/frontend_urls.py`, `src/events/utils/schedule.py`, `src/events/utils/name_matching.py`, `src/events/service/event_service.py`, `src/events/service/event_update_service.py`, `src/events/utils/visibility.py`, `src/events/utils/visibility_settings.py`
- context: `src/events/service/event_manager/**`, `src/events/service/membership_manager/**`, `src/events/utils/hostname.py`, `src/events/utils/membership_access.py`, `docs/architecture/eligibility-pipeline.md`
- risk: HIGH
- tier: opus
- security focus: the event/organization visibility matrix (public, members-only, private, staff-only, attendee-only fields; address and cancellation-reason gating via `attending()`); `events/utils` visibility queryset rebuild and its call sites (signals, bulk paths); anonymous discovery endpoints (search, filters, geo, calendar feeds) leaking private rows; attendee and questionnaire exports; dashboard and follow endpoints crossing organizations; user preferences and dietary data exposure; signals that rebuild visibility.
- debt focus: events/models/event.py (931 lines) and filters.py (571) size; schema/__init__.py re-export bloat; signals.py (934 lines) mixing concerns; venue_service size.

## events-series-recurrence-waitlist
- globs: `src/events/service/recurrence_service.py`, `src/events/service/waitlist_service.py`, `src/events/service/series_pass_service.py`, `src/events/service/series_pass_purchase.py`, `src/events/service/series_pass_file_service.py`, `src/events/service/series_pass_questionnaires.py`, `src/events/controllers/event_series.py`, `src/events/controllers/event_series_admin.py`, `src/events/controllers/series_pass.py`, `src/events/controllers/series_pass_admin.py`, `src/events/controllers/organization_admin/recurring_events.py`, `src/events/controllers/event_admin/waitlist.py`, `src/events/controllers/event_admin/waitlist_offers.py`, `src/events/models/event_series.py`, `src/events/models/recurrence_rule.py`, `src/events/models/series_pass.py`, `src/events/schema/event_series.py`, `src/events/schema/recurrence_rule.py`, `src/events/schema/recurring_event.py`, `src/events/schema/series_pass.py`, `src/events/schema/waitlist.py`, `src/events/tasks/recurrence.py`, `src/events/tasks/series_pass.py`, `src/events/tasks/waitlist.py`, `src/events/admin/series_pass.py`, `src/events/admin/waitlist_offer.py`, `src/events/utils/recurrence_validators.py`, `src/events/service/event_series_service.py`
- context: `src/events/service/batch_ticket_service/**`, `src/events/service/ticket_service.py`, `docs/architecture/recurring-series.md`, `docs/architecture/series-passes.md`, `docs/architecture/waitlist.md`
- risk: HIGH
- tier: sonnet
- security focus: series pass purchase and redemption (pass holders getting seats they should not, bulk paths skipping the visibility rebuild); waitlist offer expiry, ordering and claim races; recurrence generation limits (unbounded event creation); BOLA on series and pass ids.
- debt focus: recurrence_service and series_pass_service size; duplicated eligibility between pass and single-ticket paths.

## events-management-seeders
- globs: `src/events/management/__init__.py`, `src/events/management/commands/__init__.py`, `src/events/management/commands/README.md`, `src/events/management/commands/README_TEST_EVENTS.md`, `src/events/management/commands/benchmark/**`, `src/events/management/commands/bootstrap_helpers/**`, `src/events/management/commands/demo_video_helpers/**`, `src/events/management/commands/seeder/**`, `src/events/management/commands/bootstrap_demo_video.py`, `src/events/management/commands/bootstrap_events.py`, `src/events/management/commands/bootstrap_perf_tests.py`, `src/events/management/commands/bootstrap_test_events.py`, `src/events/management/commands/enable_free_tier_cancellation.py`, `src/events/management/commands/org_nudges.py`, `src/events/management/commands/reset_events.py`, `src/events/management/commands/run_benchmark.py`, `src/events/management/commands/seed.py`
- context: `src/events/models/**`
- risk: LOW
- tier: sonnet
- security focus: only prod-facing commands matter; seeders and benchmarks must never run with real credentials or in production (guards on DEMO_MODE/settings); no real data in fixtures.
- debt focus: bootstrap and benchmark modules are test scaffolding; flag dead helpers and duplication, not style.

## accounts
- globs: `src/accounts/**/*.py`
- context: `src/common/authentication.py`, `src/common/auth_base.py`, `src/common/throttling.py`, `src/oauth/**`, `docs/architecture/auth-sessions.md`
- risk: CRITICAL
- tier: opus
- security focus: registration and login (Turnstile, enumeration resistance, global bans on every entry path including OIDC and guest); JWT issue, refresh rotation, reuse detection and lineage logout; two token worlds (blacklist also revokes OAuth tokens); OIDC relying-party flows (state, nonce, account linking, pre-account hijack); 2FA/OTP; impersonation; GDPR export and erasure; referral payouts and Stripe referral onboarding; tenant sessions on custom domains.
- debt focus: accounts/service size; admin module size (1.9k lines); duplicate token code between jwt.py and service.

## oauth-provider
- globs: `src/oauth/**`
- context: `src/accounts/jwt.py`, `src/events/controllers/permissions.py`, `docs/developer-guide/oauth.md`, `docs/adr/0018-oauth-oidc-provider.md`
- risk: CRITICAL
- tier: opus
- security focus: authorization code and refresh grants (PKCE, redirect URI matching, scope narrowing); scope gate runs before any owner or staff shortcut so app tokens never exceed granted scopes; app registration and ownership; token revocation and the `run_before` migration quirk; consent screens; guards on the many controllers that accept app tokens.
- debt focus: validator and service duplication; scope table drift versus controllers.

## integrations
- globs: `src/integrations/**`
- context: `src/events/models/event.py`, `docs/adr/0017-platform-listings-provider-protocol.md`
- risk: MEDIUM
- tier: sonnet
- security focus: third-party OAuth token storage (encrypted fields), webhook authenticity from external platforms, SSRF through provider URLs, Revel remaining source of truth (counts only flow back), cross-organization credential use.
- debt focus: provider protocol adherence; duplicated HTTP/error handling across providers.

## notifications
- globs: `src/notifications/**/*.py`
- context: `src/templates/**`, `docs/architecture/notifications.md`, `docs/architecture/email-sending.md`
- risk: MEDIUM
- tier: sonnet
- security focus: audience resolution (never notify non-attendees of attendee-only data; PENDING buyers get updates but not attendee-only info); per-type and per-channel opt-outs, mandatory vs optional types, suppression list; unsubscribe and List-Unsubscribe handling; language override in Celery; context schemas leaking PII across recipients; tenant-correct sender identity.
- debt focus: signals package (2.5k lines) and service (5.6k lines) size; enum/context-schema/template triple maintenance; i18n literals.

## questionnaires
- globs: `src/questionnaires/**`, `src/events/controllers/questionnaire/**`
- context: `src/events/service/event_questionnaire_service.py`, `src/events/service/membership_questionnaire_service.py`, `docs/architecture/questionnaires.md`
- risk: HIGH
- tier: opus
- security focus: submission and evaluation integrity (score tampering, re-submission, cooldowns, per-user ownership of submissions); LLM evaluation inputs (sanitization is accepted as is, report only a concrete new bypass); organizer-only access to answers; assignment of questionnaires to events, series and memberships; BOLA across organizations.
- debt focus: schema.py (893) and models.py (795) size; evaluator branching; duplicated admin code.

## polls
- globs: `src/polls/**`
- context: `src/events/models/ticket.py`, `docs/architecture/polls.md`
- risk: MEDIUM
- tier: sonnet
- security focus: eligibility to vote (attendance definition, membership), one-vote-per-user enforcement under races, results visibility before close, organizer-only management, anonymous poll deanonymization.
- debt focus: service and controller duplication; eligibility logic copies.

## telegram
- globs: `src/telegram/**`
- context: `src/notifications/**/*.py`, `docs/guides/telegram-bot.md`
- risk: MEDIUM
- tier: sonnet
- security focus: account linking (binding a Telegram id to the right user, no hijack by replaying a link code); webhook authenticity; commands acting with user authority; notification keyboards leaking attendee-only data.
- debt focus: router duplication; keyboard and notification-keyboard overlap.

## wallet
- globs: `src/wallet/**`
- context: `src/events/models/ticket.py`, `src/events/service/ticket_service.py`
- risk: HIGH
- tier: sonnet
- security focus: pass issuance only for attending tickets and the owning user; pkpass signing and certificate/key handling (never logged or served; container file permissions); Google Wallet JWT claims; pass update endpoints and web-service auth tokens; branding guard on generated passes.
- debt focus: apple/google duplication; resolution logic overlap with ticket service.

## geo-moderation
- globs: `src/geo/**`, `src/moderation/**`
- context: `src/accounts/service/global_ban_service.py`, `docs/architecture/moderation.md`
- risk: LOW
- tier: sonnet
- security focus: IP-derived location handling and header trust (client IP resolution); public city search abuse; moderation guard fail-open behaviour; blocklist data integrity.
- debt focus: small and mostly settled; confirm the blocklist guard is actually wired and tested.

## api-common-config
- globs: `src/api/**`, `src/common/**/*.py`, `src/revel/**`, `src/manage.py`, `src/gunicorn.conf.py`
- context: `pyproject.toml`, `../infra/Caddyfile`, `../infra/**/docker-compose*.yml`, `.github/workflows/**`, `Dockerfile*`, `docs/architecture/auth-sessions.md`, `docs/engineering-notes.md`
- risk: HIGH
- tier: opus
- security focus: authentication classes and token validation; throttling classes and the DISABLE_THROTTLING flag; client-IP and proxy-header trust (must match the Caddyfile); middleware (language, observability, testing flag); settings (DEBUG, ALLOWED_HOSTS, CORS/CSRF, cookie flags, secret and key handling, `SYSTEM_TESTING` accepted by design); upload and thumbnail services (file type, size, malware scan, EXIF); sanitizers and markdown rendering (XSS); encrypted fields and key rotation; signing helpers; global exception handlers (no internal detail leakage); Django admin exposure and sidebar; Celery config; observability endpoints.
- debt focus: settings sprawl; common/tasks.py (511 lines); unfold admin config (686 lines); dead utilities in common/utils.

## templates
- globs: `src/templates/**`, `src/notifications/templates/**`, `src/accounts/templates/**`, `src/events/templates/**`, `src/common/templates/**`
- context: `src/notifications/context_schemas.py`
- risk: MEDIUM
- tier: sonnet
- security focus: autoescape bypass (`|safe`, `mark_safe`) on user-controlled values in email, in-app, telegram and invoice/PDF templates; links built from untrusted hosts (must use org-aware or platform links); PII in templates; invoice template fields required by compliance.
- debt focus: copy-pasted email layouts across notification types; unused templates with no matching notification type.

## assets-and-data
- globs: `src/assets/**`, `src/fonts/**`, `src/locale/**`
- risk: LOW
- tier: sonnet
- security focus: nothing dynamic; check only that no secrets, certificates or personal data are committed, and that `.po` files carry no fuzzy entries.
- debt focus: untranslated or stale catalog entries (`make i18n-check` is the authority).

## Specialists (debt sweeps, cross-cutting)
- dependencies: `pyproject.toml`, `uv.lock`, `Makefile` audit targets, `docs/runbooks/dependency-audit.md`
- tests: `src/**/tests/**` (metric-driven: slowest tests, sleeps, mock density, vacuous assertions), sampled
- consistency: house rules from `CLAUDE.md` across regions (thin controllers, no model-to-service imports, pinned task names, `_()` on user-facing errors, `on_commit` dispatch)
