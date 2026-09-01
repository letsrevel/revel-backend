# What's New — August 1 to September 1, 2026

A month of finishing what last month started. **Buying tickets is now one cart** — mix as many
tiers as you like, seated and unseated, and pay once. **Google Wallet** arrived, **membership
cards** became a real credential you can scan at the door, and **organizers can now refund from
inside Revel** instead of detouring through the Stripe Dashboard. Plus a serious pass over VAT
correctness, and hundreds of translations that were quietly still in English.

## 🎟️ Events & Tickets

- **One cart, one payment.** Buyers can put any mix of ticket tiers into a single cart —
  including seated and general-admission tiers together — and check out through one Stripe
  session, instead of repeating the purchase dialog per tier. Tier cards get +/− steppers, a
  sticky summary bar shows the running total and seat-hold countdown, and one **Buy** button ends
  it (`POST /events/{event_id}/checkout`, plus a `/checkout/public` twin for guests).
- **The checkout sheet is now skippable.** When there's nothing to ask, it doesn't ask: one sheet
  collects per-ticket names, pay-what-you-can amounts, zone and accessible-seating choices, a
  cart-level discount code, a multi-item VAT preview, and billing details — only where they
  apply. The discount code reports honestly which tiers it actually applies to.
- **Guests get the full experience**: the same steppers, seat picker, and discount codes, with an
  email-confirmation step for carts not paid online — now covering multiple tickets across
  multiple tiers in one confirmation.
- **Ticket holder names are optional.** Untick **Require ticket holder names** on a ticketed
  event (`require_ticket_names`, default on) and logged-in buyers check out with zero extra
  input, while guests need only an email. Names can be added or corrected later — by the ticket
  owner from **My tickets**, or by admins from the event tickets table — and admin search matches
  holder names. Renaming regenerates the PDF and wallet pass.
- **Per-event ticket limits actually mean something now.** `max_tickets_per_user` on an event is
  enforced *across* its tiers rather than being overridden by each tier, so a buyer can no longer
  hit the per-tier cap again on every tier. Both limits apply, strictest wins. A data migration
  preserves every existing event's effective allowance exactly — nothing you already run changed
  shape. Buyers now see a plain "N tickets left for you at this event" line, computed from the
  new `event_remaining` field on `my-status`.
- **Tier sale windows are enforced server-side.** `sales_start_at` / `sales_end_at` are now
  checked on every purchase path, closing a gap where a direct API call could buy outside the
  window. Guest checkout rejects a closed tier at submission instead of emailing a link that can
  no longer work.
- **Embed builder**: a new **Embed** tab under org admin makes embeddable events reachable
  without hand-writing anything. Choose the org's event list, a single event, or a series; set
  theme, language, frame title and sizing; tune list filters; watch a live preview. Copy either
  the loader script or a plain iframe for CMSes that strip `<script>`. Only content an anonymous
  visitor can load is offered, so a copied snippet can't render an error on your own site.
- Event cards gain an **Embed on your site** action that opens the builder pre-pointed at that
  event.
- New events keep the end date and time entered at creation — the create request used to drop it,
  making every new draft a 24-hour event until the next save.
- The announcement composer's event picker is now a searchable typeahead. It was a plain dropdown
  capped at 100 events, so in larger organizations every event past the cap was invisible and
  could never receive an announcement.

## 🪑 Seating & Venues

- **Rows can curve.** Theater-style arcs, staggering, and left/center/right alignment, with
  per-row overrides — no longer just rectangular grids. Every save bakes an explicit position for
  every seat, so the buyer map renders exactly what you laid out.
- **The sector editor is WYSIWYG**: seats appear at their real coordinates with a live geometry
  panel alongside, so what you edit is what checkout draws.
- **Adjust seats mode**: drag or arrow-key nudge individual seats, rotate them, append seats to a
  row, add seats anywhere, remove them — with full session undo/redo (`Cmd/Ctrl+Z`). Seat
  rotation reaches buyers too (`seatRotations` in the public chart metadata), rendered as a
  seat-back notch on the checkout map.
- When adjusted seats no longer fit a drawn sector outline, a dialog offers to auto-fit or clear
  the outline rather than failing the save.
- **Seat holds now live as long as your cart**, with a visible countdown, and the venue overview
  map hands selected seats straight into the cart. The hold ceiling follows the layered ticket
  caps, so buyers can hold exactly as many seats as they're allowed to buy.
- All seat surfaces — designer, editor, checkout map — now share one visual language: dark
  poster-ink panel, solid price-category dots, dimmed unavailable seats (the X marks are gone), a
  circular selection ring, and an amber keyboard-focus ring.
- Seats next to an aisle no longer sit one unit apart between the editor and the buyer map; the
  legacy editor stored aisle-shifted positions with an off-by-one. Re-saving a sector with aisles
  applies the correction.

## 📱 Wallet Passes & Membership Cards

- **Add to Google Wallet.** Android users get a first-class wallet pass for tickets and series
  passes alongside the existing Apple one (`GET /tickets/{id}/wallet/google`), with the official
  localized badge artwork in ticket and payment-confirmation emails. On Android the Google button
  is listed first, on iOS and desktop the Apple one — nothing is ever hidden. PDF remains the
  universal fallback.
- **Membership cards.** Every membership on **My Memberships** gets a **Show card** button:
  organization branding, name and pronouns, tier, member-since date, QR code, plus Apple Wallet,
  Google Wallet and PDF where configured. The card is an *identity credential, not a ticket* — it
  never admits anyone by itself, its validity is checked at the moment it's scanned, and it's
  shown for paused, cancelled and banned memberships too, each stating in words what a scan will
  report.
- **Verify a card at the door.** Organizations gain a scanner at **Members → Verify a card** that
  works outside any event — a clubhouse door, a members' night. Each scan shows the member's
  photo, name, pronouns, tier and true status, with paused/cancelled/banned called out explicitly
  rather than reported as a failed lookup. The camera stays live between scans and a manual code
  field is always available. Requires the **check in attendees** permission.
- **Membership cards work at event check-in too.** The event scanner accepts them: a member
  holding exactly one ticket for the event is checked in on the spot; otherwise nothing is checked
  in and staff get a report of who the person is, whether their membership is live, and which
  tickets they hold — each with its own Check in button.
- Google Wallet save links no longer break when a logo or cover image is replaced after the link
  was issued — passes embed stable image URLs that always serve the current image.
- Cancellation emails no longer attach a still-scannable wallet pass for the cancelled ticket.
  Passes and save links are only offered while a ticket is active or pending, and pending
  pay-at-the-door tickets show a "Pending payment confirmation" pill next to their downloads.
- Wallet passes and PDFs carry a discreet "Powered by Revel" attribution.

## 💳 Payments, VAT & Refunds

- **Organizers can refund from inside Revel.** With `manage_tickets`, refund online (Stripe)
  tickets — full or partial — via a refund dialog showing amount paid, refunds so far, quick
  amount suggestions, an optional internal reason, and refund history
  (`POST .../tickets/{id}/refund`).
- **Refund no longer means cancel.** The two are orthogonal: a refunded ticket stays valid and can
  carry a partial-refund badge, and a refund issued from the Stripe Dashboard is recorded but
  leaves the ticket active — the organizer cancels explicitly. Cancelling a ticket can optionally
  include a refund.
- **Cancelling an event can refund everyone.** Opt into **Refund all tickets** and a background
  sweep refunds every online ticket, with a per-currency preview against your available Stripe
  balance, a staff summary when it completes, and a **Retry refunds** resume button. Un-cancelling
  is blocked once refunds start.
- Partial refunds produce amount-aware credit notes with pro-rata VAT, and revenue reports book
  each refund in the period it actually succeeded.
- **Admission VAT is charged where the event takes place.** Buyers of physical-event tickets
  always pay the full gross price at the organizer's rate — EU B2B reverse charge and non-EU
  zero-rating no longer apply to admission, and a self-declared non-EU billing country no longer
  buys a discount. VIES validation still runs so a verified buyer VAT ID prints on B2B invoices.
  Historical invoices re-render exactly as issued.
- **Virtual events get their own VAT treatment.** The event form gains a Taxes section with a
  **Virtual event** checkbox and a VAT-country override, with an effective-country readout and a
  mismatch warning suggesting a tax adviser (`Event.is_virtual`, `Event.vat_country_code`;
  effective country resolves venue city → event city → organization). EU B2B with a validated VAT
  ID gets reverse charge, non-EU no EU VAT, EU B2C the organizer's rate as an interim treatment,
  flagged on previews and invoices. For virtual events the address field becomes "Address or
  meeting link", and a meeting link renders as a clickable "Virtual event" row instead of leaking
  a raw URL.
- **Mixed-rate invoices stop claiming a single rate.** Attendee invoices now carry
  `has_mixed_vat_rates` and a `vat_breakdown` — one net/VAT/gross bucket per rate, summed from
  the line items. A multi-tier cart can genuinely mix 22% and 10% items; the invoice PDF, the
  account invoices page, the org-admin view and the Django admin now show the per-rate table
  instead of a scalar rate that only ever named the first line's.
- **Invoice totals are derived, not stated.** Editing a draft invoice recomputes
  `total_gross` / `total_net` / `total_vat` from its line items, and the request now rejects
  those fields (and unknown fields) with a 422 rather than a 200 that silently ignored them. A
  draft can no longer be issued with header totals contradicting its own lines — the DRAFT →
  ISSUED transition is refused for any drifted row, so it can't become a legal document.
- Invoice PDFs no longer subtract the discount twice; `total_gross` is already net of it, so the
  discount is now an informational note rather than an arithmetic row that left the column not
  footing.
- **Organizer payout transparency**: online ticket tier forms show a "You get: ~€X per ticket"
  net-payout preview computed from your organization's actual fee rates and VAT treatment, and
  the landing-page fee calculator gains an "I'm charged VAT on the platform fee" checkbox.
- **Stripe data minimization.** No organizer-authored content is sent to Stripe, uniformly for
  everyone: checkout line items use constant `Ticket` / `Season pass` labels (no event, tier, or
  holder names), membership products are named `Membership`, customers are created without a
  display name (email kept for receipts), and return URLs carry UUIDs instead of slugs. A
  `scrub_stripe_products` command (with `--dry-run`) cleans up previously materialized products.
- A cart exceeding a discount code's per-user usage limit is now rejected outright instead of
  silently applying the discount to fewer tickets than requested. Cart uniformity is re-verified
  on row-locked tiers at checkout, so an organizer edit landing mid-checkout can't produce a cart
  priced on stale assumptions.
- `GET /api/me/billing` returns `200` with `null` instead of `404` when no billing profile
  exists, and paid checkout no longer fires a failed billing request for buyers who never saved
  billing details.

## 🎨 Brand, Language & Interface

- **The landing page is a full-bleed color-block poster**: brand-gradient hero with a rotating
  headline noun, amber pricing panel with a direct fee comparison, dedicated panels for
  communities and for venues/theaters, ticket-stub feature stubs, and an open-source panel.
- **The rebrand reached the printed and mailed surfaces**: ticket PDFs redesigned as a
  boarding-pass with a tear-off QR stub, the seat in display type (`Seat C8` / `Row C · Balcony`),
  always one A4 page. Membership card PDFs moved onto the same design system. Email headers and
  CTAs use the vertical purple → crimson gradient, destructive-action buttons use the deep crimson
  `#E4251B` for WCAG AA contrast behind white copy, and invoice / credit-note / payout / VAT-report
  PDFs use the brand Ink color.
- The **let's revel.** logo now looks the same everywhere — the navbar, footer and landing panel
  each drew it differently, with varying letter-spacing, three different full-stop colors, and
  the word "revel" missing its brand gradient throughout.
- **Hundreds of strings were still English.** 661 German and 589 Italian strings shipped as the
  untranslated original and are now translated, with terminology matched to Revel's emails.
- **Gender-inclusive wording across all six languages**: German follows the gender-star
  convention consistently — including the SEO landing pages a catalog sweep never reached — and
  drops the ungrammatical `Mitglieder*innen`. French uses neutral rephrasing with the point médian
  as fallback; Italian, Spanish and Portuguese use neutral rephrasing only, no glyphs, since
  screen readers mangle them.
- **Dates and times are properly localized.** Month and weekday names follow the UI language
  everywhere (German previously showed half-English "Fr., Okt. 24 • 20:00"), ranges use
  locale-aware punctuation, ranges straddling a DST change label each end with its own timezone
  abbreviation, and Portuguese short dates keep a textual month.
- Dashboard RSVP and ticket cards, invitation lists and questionnaire assignments now show times
  in the **event's** timezone rather than silently using the viewer's clock (`timezone` on
  `MinimalEventSchema`).
- RSVP deadlines are counted in calendar days in your timezone — a deadline the day after
  tomorrow no longer reads "in 1 day", single steps use the locale idiom ("tomorrow",
  "übermorgen"), sub-24-hour deadlines keep hour-level urgency, and the doubled preposition
  ("RSVP by in 6 days") is gone.
- **Accessibility**: keyboard focus is now clearly visible on SEO landing-page CTAs (white ring
  over a purple gap, identical in both themes), and status pills announce their full description
  ("Membership status: Active") instead of having it silently discarded by screen readers.
- **Mobile fixes**: admin tab strips no longer push their first and last tab outside a phone
  viewport, sticky admin bars follow the real header height instead of stacking into each other
  (and are dropped entirely under 640px tall), and organization settings and the embed builder no
  longer scroll sideways.
- **Download PDF** behaves identically on tickets, series passes and membership cards; ticket
  PDFs save in place with an event-named file (`<event-slug>-ticket.pdf`) instead of opening a
  tab popup blockers could suppress. Downloads no longer occasionally fail in Firefox and Safari,
  and failures show a translated message rather than raw technical text.
- Pages no longer appear unstyled after a deployment on iOS Safari, which had been restoring a
  cached copy referencing assets no longer served; long-lived tabs now reload fully on the next
  navigation after a deploy.
- Pending-ticket notifications no longer show the "Payment Instructions" heading twice, and
  plain-text ticket-update emails no longer render a broken template tag in place of the tier
  name.

## 🛠️ Under the Hood

- **Dashboards got dramatically faster.** A redundant `DISTINCT` was costing roughly 600ms of
  *query planning* per request on ticket, RSVP, invitation and organizer tier lists. The OpenAPI
  schema is now built once per process instead of on every `/api/openapi.json` hit,
  `my-permissions` is served from a short-lived cache invalidated the moment memberships or staff
  permissions change, and Django admin Event pages no longer render every seat, tier and user in
  inline dropdowns.
- New `REDIS_SOCKET_CONNECT_TIMEOUT` / `REDIS_SOCKET_TIMEOUT` settings (default 1s) keep a hung
  Redis from blocking requests — worth setting deliberately if you self-host.
- Public `GET /api/organizations/{uuid}` retrieves an organization by UUID with the same
  visibility rules and payload as the slug route.
- The four per-tier checkout endpoints are **deprecated** in favor of the cart endpoints. They
  still work for now; if you integrate against the API directly, this is the moment to migrate.
- A `report_mischarged_attendee_vat` command emits a CSV of historical invoices affected by the
  previous attendee-VAT treatment, for review with a tax adviser.
- Seeded organizations get deterministic placeholder logos, so seeded and demo instances render
  with real imagery instead of blank avatars.
- **Security**: dependency floors raised for freshly published CVEs across `aiohttp`,
  `cryptography`, `h2`, `sqlparse` and `pip`, and production and development dependencies were
  refreshed across the board.

**📋 Full changelogs:** [Backend](https://github.com/letsrevel/revel-backend/blob/main/CHANGELOG.md) · [Frontend](https://github.com/letsrevel/revel-frontend/blob/main/CHANGELOG.md)
