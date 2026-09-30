# What's New — September 2 to September 30, 2026

A month of opening Revel up to the rest of your stack. **Eventbrite listings sync from Revel**,
**sign-in works with any OpenID Connect provider**, and Revel is now an **OAuth / OpenID Connect
provider** itself, so apps and AI assistants can act for you with scoped, revocable access.
**Organization mail now comes from your organization**, with one-click unsubscribe and
per-organization muting. Tiers get their own **check-in windows**, sales can be **attributed to
campaigns** without tracking anyone, and a long list of refinements and a security review landed
alongside.

## 🔗 Integrations & Sign-in

- **Eventbrite listing sync.** Connect your Eventbrite account from the new owner-only
  **Integrations** page and list an event on both platforms without retyping it. Revel stays the
  source of truth: a push creates an Eventbrite **draft** (venue, description, eligible tiers),
  publishing is a separate explicit step, and remote edits are never pulled back. Opt into
  **auto-sync** per connection or per event, and it keeps the listing current without ever
  publishing on its own.
- **Import from Eventbrite**: bring an existing Eventbrite event into Revel as a draft. Each import
  is a trackable job that reports progress and a clear reason if something fails.
- **One sold count per tier.** Each tier shows sales on every connected platform, fed by the
  platform's webhooks and reconciled on a schedule. A new **Sales paused** switch stops one tier
  without deleting it or changing its dates, on Revel and on connected platforms alike.
- **Sign in with any OpenID Connect provider** (Google, Keycloak, Authentik, …) via "Continue
  with …" buttons on login and registration, configured through `OIDC_PROVIDERS`. Manage linked
  providers under **Account → Security**. New accounts pick up the provider's profile picture,
  which goes through the same scanning and thumbnailing as an upload. Self-hosters: this replaces
  `FEATURE_GOOGLE_SSO`.
- **Revel is now an OAuth 2.1 / OpenID Connect provider.** Third-party apps and MCP hosts can act
  on your behalf with scoped, revocable tokens, and "Sign in with Revel" works via `id_token` and
  `/o/userinfo`. A consent page shows exactly what an app asks for, with payment-related
  permissions marked. **Connected apps** lists every app with access and removes it in one click,
  and **Developer apps** lets you register your own. An app can only ever do what you can already
  do yourself, and only what its consent screen named. Available on deployments that configure a
  signing key (`features.oauth_provider` in `/version`).
- Changing your email address or resetting your password now disconnects every connected app,
  and a password reset signs you out everywhere. Both are account-recovery moments, so anyone who
  held access before has to be granted it again.

## 🎟️ Events, Tickets & Check-in

- **Per-tier check-in windows.** Saturday-only and Sunday-only tickets for one event? Give each
  tier its own "Check-in opens / closes" (`check_in_opens_offset` / `check_in_closes_offset`),
  stored relative to the event start so it holds up when you reschedule, duplicate or repeat the
  event. Tier cards and tickets show an "Entry: …" window in the event's timezone.
- **Campaign attribution with zero tracking.** The `utm_*` tags already on a landing or embed URL
  travel through browsing into checkout and are stamped on the ticket. A **Sales by source**
  breakdown on the tickets admin page shows which newsletter, post or partner embed each sale came
  from, and the same breakdown is available org-wide. Nothing is stored on the visitor's device:
  no cookie, no local storage, no referrer. (API: the series-pass checkout body is now
  `{"billing_info": …, "attribution": …}`.)
- **Guest invitation links.** Visitors holding an event invitation link can **continue as a guest**
  and buy from the invited-only tiers the link unlocks, through guest checkout and RSVP, without
  creating an account.
- Guest checkout gives more helpful guidance: a guest whose email already has an account gets
  sign-in buttons with the address prefilled, and a cart too large to confirm by email offers
  **Split your purchase**, which returns you to the cart with everything still in it. Tiers a
  visitor can't buy now show a disabled button with the reason before checkout starts.
- **Refund terms up front**: each tier in the checkout sheet states its refund brackets,
  cancellation deadline and any fee, or that it's non-refundable, before the buyer pays.
- Prices are formatted for your language everywhere in ticketing and checkout, pay-what-you-can
  tiers lead with their price range, and logged-out visitors see **Get Tickets** when guest
  checkout is available.
- Numeric and duration fields across the app (ticket limits, questionnaire scores, seat grids,
  refund brackets…) can now be cleared and retyped freely. Values settle when you leave the field,
  and a duration keeps the unit you picked.
- Seats you pick on the map now stay in the cart summary reliably after you confirm the
  selection.

## 💳 Payments, Refunds & Invoices

- **Record refunds on offline and at-the-door tickets.** When you cancel a paid offline or door
  ticket, you can record a full or partial refund you returned yourself. It shows up in the
  event's revenue figures, and a cancellation without a refund keeps counting as a sale in
  revenue and VAT reports.
- **Credit notes for buyers.** `/account/invoices` lists every credit note with its own PDF.
  Fully refunded invoices stay listed as "Cancelled / credited", and partly refunded ones are
  marked "Partially credited".
- **Box office knows how a ticket was sold**: tickets record a `sale_source`, so comps read
  "Comp" and door sales "Door sale". The sell panel shows door staff exactly what to collect for
  the chosen seat and tier.
- **Fixed-amount discount codes scoped to tiers** take their currency from those tiers.
- Resuming a paused online tier now runs the same Stripe Connect check as creating one, so a
  paid tier only goes live when buyers can actually pay.
- Checkout stays fast even when the EU VAT-number service (VIES) is slow: the lookup on the
  payment path is capped at 2 seconds.
- Fee copy is clearer: the fixed platform fee is per transaction, and the tier form's net-payout
  estimate says it assumes a single-ticket order.

## 🔔 Email & Notifications

- **Organization mail comes from your organization.** Announcements, invitations, event updates
  and reminders go out as `"<Org> via Revel"`, with your verified contact address as Reply-To.
  Self-hosters can put this mail on its own sending domain (`ORG_EMAIL_DOMAIN`).
- **One-click unsubscribe** (RFC 8058) on organization mail and digests, so mail apps show their
  native unsubscribe button. The unsubscribe page now stops only the kind of email the link came
  from, and lists what always arrives.
- **Mute one organization** from its page or your follow menu. This stops its announcements on
  every channel while leaving them readable on the page. Manage the list under **Account
  settings → Muted organizations**.
- **Receipts are always delivered.** Payment, ticket and subscription receipts, and legal or
  platform notices, arrive regardless of notification settings. Platform notices are never used
  for promotion.
- People invited without a Revel account get a visible **"Stop these emails"** link, and
  organizations can invite up to 200 new addresses per day (`PENDING_INVITATION_DAILY_CAP`). The
  invite dialog explains a limit and keeps your list so you can trim and retry.
- **Bounce and complaint handling**: hard bounces, spam complaints and invalid addresses are
  recorded (optional Brevo webhook) and no longer emailed. If that happens to your address, a
  banner explains why and how to fix it, and your tickets and receipts stay in the app.
- **Preferences now apply consistently across every delivery path.** Digests follow "silence
  all" and per-type opt-outs, the follow menu's announcements toggle is the per-organization mute
  (existing opt-outs carried over), and turning email back on restores every type. Unsubscribe
  links now stay valid for years rather than weeks.
- Referral program emails and Telegram's `/unsubscribe` are working again.

## 🤝 Referral Program

- **Apply to become a referral partner** at `/referral/apply`: a short explainer, a form, and
  clear outcomes. Admins approve or reject with an optional note and can invite partners by
  email. Referral codes are case-insensitive and keep the capitalisation you typed. Per-partner
  revenue share overrides are supported, and the self-billing agreement can be accepted from the
  payout page.

## 🎨 Interface, Language & Landing Pages

- A new **club membership** landing page and a **For clubs & studios** home-page panel speak to
  gyms, studios, dance schools and choirs, in all six languages.
- SEO landing pages were rewritten in all six languages to match what Revel does today, now
  including reserved seating, guest checkout, waitlists, invoices, recurring series and lifetime
  memberships.
- When a backend is briefly rate-limited or busy, public pages show a localized "Just a Moment"
  page with a retry hint.
- Signing in from a Follow, RSVP, invitation, join or error page takes you back where you started.
  Having Revel open in several tabs keeps you signed in in all of them.
- Failed saves are now reported consistently across the app: the tier editor keeps the dialog
  open with the error, toggles roll back, and potluck, questionnaire and poll pages name the
  actual problem.
- Accessibility and mobile: required questionnaire fields are announced to screen readers, org
  admin tabs wrap instead of being cut off, and admin grids and checkout footers fit phones.

## 🔒 Security

We ran a dedicated security review this cycle and shipped everything it surfaced. We list the
findings so self-hosters know what to update:

- Access rules are stricter and consistent: global bans are enforced on guest sign-up and password
  reset. Bans and hard blacklists hide an organization's content in every access path. Lapsed or
  banned members lose members-only resources. A series pass's coverage now follows each event's
  admission type, and event status changes require the `manage_event` permission.
- Integrity under load: the per-user ticket cap holds under concurrent checkouts, and
  multi-answer questions contribute at most their own weight to automatic scoring.
- Content safety: admin views escape organizer-supplied names, spreadsheet exports neutralize
  formula-like cells, image dimensions are bounded before processing, and the malware quarantine
  handles re-uploads and questionnaire files reliably.
- Rate limiting: each limit keeps its own counter, and signed-in account and two-factor routes
  have per-user limits.
- Tokens and secrets (reset, verification, unsubscribe, OAuth, invitation links) are redacted from
  tracing spans and proxy access logs. The OAuth apps of a globally banned user are deactivated
  with them.
- Dependencies updated for published advisories, including `weasyprint`, `pyjwt`, `oauthlib`,
  `soupsieve`, `urllib3` and `pip`.

## 🌍 Self-hosting & Infrastructure

- `setup.sh` can now turn on the **OAuth / OIDC provider**, an **organization sending domain** and
  the **bounce-webhook secret**. It also sets bind-mount ownership for the container user
  automatically, and the README was rewritten around the wizard, tiers and profiles.
- Redis uses a `volatile-lru` eviction policy with alerts on evictions and out-of-memory. The
  Django cache gets its own logical database (`CACHE_REDIS_DB`, default `2`), and Telegram state
  now expires (`AIOGRAM_FSM_TTL_SECONDS`).
- `manage.py check` flags a missing or non-public `BASE_URL` (production must set it explicitly),
  a non-HTTPS base URL, and self-hosted instances sending mail as `letsrevel.io`.
- Deployments start up without an IP2Location database: nearest-first sorting switches off with
  a warning and turns itself on once the file appears. Server-side session refresh uses the
  internal API address in containerized setups.
- `STRIPE_PUBLISHABLE_KEY`, `TELEGRAM_SUPERUSER_IDS` and `TELEGRAM_STAFF_IDS` are no longer used
  and can be dropped from your environment.

## 🛠️ Under the Hood

- **The OpenAPI schema now declares every error response**: `401`/`403` on all authenticated
  operations and `422` on everything that parses input. Guest checkout and wallet-pass errors
  carry machine-readable `code`s. Generated clients can now handle these responses by type.
- 76 more user-facing error messages are translated, and the build blocks any new untranslated
  one.
- Admin: organizations can be filtered by Stripe status, events, VAT and visibility and sorted by
  payment volume, and the Eventbrite admin pages have their own sidebar group.
- `make demo-video` seeds the scenarios our product videos are recorded against, complete with
  logos and cover art.

**📋 Full changelogs:** [Backend](https://github.com/letsrevel/revel-backend/blob/main/CHANGELOG.md) · [Frontend](https://github.com/letsrevel/revel-frontend/blob/main/CHANGELOG.md)
