# What's New — 22 May → 22 June 2026

_Last month across backend, frontend, and infrastructure._

## 💳 Payments, VAT & Reporting

- **Full revenue & VAT reporting for organizers** — a new owner-only **Financials** page showing revenue, refunds, net income, and per-VAT-rate breakdowns by currency and event, filterable by year/quarter/month. Download a tax-precise report bundle (XLSX + PDF) for any period, or have it emailed automatically each month/quarter (`revenue_report_cadence`). One engine drives every view, so the numbers always reconcile.
- **Richer per-event financials** — now break out online (Stripe) vs at-the-door payments, VAT detail, and both online and offline refunds.
- **Ticket admin upgrades** — a sortable ticket list (tier, price, status, payment method, date — persisted in the URL), a per-ticket discount-code badge, and a "Total earned" gross/net/refunded box per currency.

## 🎟️ Events & Tickets

- **Open-ended events** — mark an event with no fixed finish; it shows "Open-ended"/"Ongoing" everywhere instead of an end time (`is_open_ended`, propagates to recurring series). The end is still tracked internally for calendar/Wallet.
- **Event schedule / timeline** — author an ordered list of display-only sessions (title, description, start offset, duration, location, optional **Required** badge), shown on the public page and copied on duplication/recurrence.
- **Event bookmarks** — privately bookmark events to find later, with a Bookmarked dashboard filter; no effect on eligibility or sign-ups.
- **Cancellation reason** — attach an optional message when cancelling; shown to participants and included in the notification.
- **Image cropping for all uploads** — event/org/series logos and banners now open a crop-and-reposition modal (PNG transparency preserved) instead of server auto-cropping.
- **Check-in resilience** — manual ticket-code entry when the scanner camera fails, with clearer per-error messages.

## 🗳️ Polls & Questionnaires

- **Polls** — a brand-new polls system: create polls with audience, anonymity, scheduling, and result-visibility controls; lifecycle actions; and a public voter page with its own nav item and dashboard tile.
- **Duplicate questionnaires & polls** — deep-copy one as a starting point instead of rebuilding by hand (clones into a DRAFT; votes and submissions are never copied).
- **Smoother review** — Next/Previous navigation when reviewing submissions, plus an instant "you're all set" confirmation when a submission is auto-accepted.

## 🔔 Notifications & Comms

- **Scheduled announcements** — send at a future time or relative to event start/end (auto-shifts if the event moves), plus an option to re-send to people who sign up later.
- **Instant transactional emails** — payment/ticket emails (confirmation, created, cancelled, refunded) now skip the digest and arrive immediately.
- **Digest overhaul** — grouped by type with a body, timestamp, and "View details" links, restyled to match the transactional emails.
- Event times in every notification and email now render in the **event's own timezone**.

## 🌍 Self-hosting & Infrastructure

- **Self-hosting support** — Revel now boots on a single self-hosted box without ClamAV, Telegram, or the full geo dataset, via feature flags (`FEATURE_MALWARE_SCAN`, `FEATURE_TELEGRAM`, `FEATURE_ORGANIZATION_CREATION`). Infra ships compose profiles, a parameterized Caddyfile, and a `setup.sh` wizard; the frontend resolves its API URL at runtime so one build can target any backend.
- **Feature flags on `GET /version`** — clients hide gated features (org creation, Telegram, Google SSO, LLM evaluation) instead of letting users hit a 403/404.
- **Observability** — a new system-health overview dashboard (with `node_exporter`), a slowest-endpoints (p95) ranking, frontend logs/metrics, and a login canary with alerts.

## 🌐 Localization & Polish

- **French** is now fully supported — translation catalog, language switcher, and French SEO landing pages (joining EN/DE/IT), with ~1,265 more strings translated across the app.
- **Rich-text editor** — every description field (events, orgs, tiers, questionnaires, polls, resources, venues, profiles, announcements) now has a WYSIWYG editor with a formatting toolbar, link dialog, and source toggle.
- Fixed the dark-mode flash (FOUC) on load, and the navbar now reflects login immediately (including 2FA) without a manual refresh.

## 🛠️ Under the Hood

- **Security hardening** — questionnaire answer-key and unscoped-list reads locked to org admins; admission-gate bypasses closed; DRAFT poll/question leaks fixed; forged-IP throttle evasion blocked; banned users can no longer reclaim invite tokens; and venue addresses no longer leak in update notifications.
- **Geolocation correctness** — "Nearest First" sorting now works in production (real visitor IP via Cloudflare, fixed IP2Location DB extraction), plus single-render JSON logging and a Loki query CLI for operators.

**📋 Full changelogs:** [Backend](https://github.com/letsrevel/revel-backend/blob/main/CHANGELOG.md) · [Frontend](https://github.com/letsrevel/revel-frontend/blob/main/CHANGELOG.md)
