# What's New — June 22 to July 31, 2026

The biggest month in Revel's history: **reserved seating**, **membership subscriptions**, and
**season passes** all shipped, the interface learned **Spanish and Portuguese**, and you can now
**embed your events on your own website**. Here's the tour.

## 🪑 Reserved Seating & Venues

Revel now does full reserved seating, end to end — from drawing your venue to seated checkout
and the box office.

- **Venue designer for organizers**: draw sector blocks, place and rotate the stage, lay out
  rows and seats, and paint seats with price categories. A coverage warning lists exactly which
  seats are still unpainted or unpriced, so you can't accidentally put an unpriced seat on sale.
- **Interactive seat picker for buyers**: a positional venue map with pan and zoom, per-seat
  prices, a price-category legend, and a live total — fully keyboard-accessible, and available
  to guests buying without an account.
- **No double-booking, ever**: selecting a seat places a short server-side hold, so two buyers
  can never claim the same seat. If someone grabs it first, you see the conflict immediately.
- **Best-available mode**: buyers can also just pick a price category and quantity and let the
  venue assign the best remaining seats (row-first scoring under the hood).
- **Per-seat-category pricing**: one ticket tier can carry different prices per seat category —
  tiers advertise their real min–max price range, and tickets record the actual price paid.
- **Box office**: door sales, comps, reseating, and assigning or overriding seats at check-in,
  with attendee debt rolled up per order.
- Demo seed data includes three showcase venues (a theatre, a comedy club, and a music hall) if
  you want to see it in action.

## 💳 Membership Subscriptions

Organizations can now sell recurring memberships — the whole pipeline, from tiers to Stripe to
member self-service.

- **Tiers and plans**: create membership tiers under org admin → **Members**, each with an
  optional gating questionnaire and an optional approval step, and attach plans — monthly,
  yearly, one-off lifetime, or free.
- **Subscribing**: members pick a tier and plan and pay through Stripe's hosted checkout; free
  and lifetime plans activate without a payment step. The membership appears the moment payment
  confirms, no refresh needed.
- **My memberships** (`/account/memberships`): every membership you hold, with plan, status,
  payment history, and change-plan / cancel / rejoin actions.
- **Applications & review**: tiers can require approval — applicants answer the questionnaire,
  organizers review in a **Requests** tab and approve, reject, or force-approve. Approval and
  rejection notifications now reliably reach the applicant.
- **Organizer visibility**: a subscription metrics strip, a Subscription-payments tab, membership
  revenue folded into org financials, staff-side revival of a lapsed membership, and migration of
  existing subscribers onto a tier. Sale caps and pausing are supported too.
- **Public membership page**: tiers now live on their own `/org/[slug]/membership` page — and
  organizations can publish a **refund policy** and **grace period** (shown to prospective members
  before they pay). Membership tiers are also drag-to-reorder (`display_order` on the tier API).

## 🎫 Season Passes

Sell a single pass covering every event in a series — with fair pricing that adjusts as the
season progresses.

- **Pro-rata pricing**: the pass price automatically drops for each event that has already
  passed (`price − passed_events × pro_rata_discount`), shown to buyers as a struck-through
  full price with "N of M events remaining".
- Buyers get a **My passes** page with a pass-level QR code, coverage progress, and PDF +
  Apple Wallet downloads; pass-derived tickets are badged "Season pass" in My tickets.
- Passes support free claims, offline reservation (pay at the venue), and Stripe checkout —
  online purchases use a single payment split penny-exact across the covered events, honoring
  each tier's VAT rate.
- Organizers manage passes from a new **Passes** tab on the series dashboard (pricing, sales
  window, quantity cap, per-event tier mapping, holders list, offline confirmation), and can
  extend a pass to later-added events — active holders get the new event's ticket free.
- Cancelling one covered event refunds each holder's share of that event; the QR check-in
  scanner accepts season-pass codes directly.

## 🎟️ Events & Tickets

- **RSVP notes**: opt in per event ("Accept a note with RSVPs", `accept_rsvp_notes`, off by
  default) to collect a short note with each RSVP — dietary info, "arriving late", anything up
  to 500 characters. Works for guest RSVPs too, and notes show up in your attendees list and
  staff notifications.
- **Attendance visibility controls**: decide separately whether guests see the attendee count,
  the capacity, and the guest list — with one-click **Open** and **Discreet** presets in the
  event wizard and recurring templates. Anything withheld is genuinely absent from the API
  response, not just displayed as a zero.
- **RSVP on behalf of an attendee**: org admins can create or edit an RSVP for someone directly
  from the attendees list — handy for phone or walk-up RSVPs.
- Attendees can now change or withdraw an RSVP on a **full** event — capacity is only enforced
  when claiming a seat, so a "yes → no" frees the spot and triggers the waitlist for the next
  person in line.
- **Two-step online checkout**: Stripe purchases now reserve your spot first, then start payment
  as a separate step — so an on-sale rush no longer queues buyers behind each other's ~2.5s
  Stripe calls. Interrupted checkouts resume cleanly via **Resume Payment**.
- Duplicating an event now prefills the new start date (rolling past dates forward) and shifts
  the waitlist cutoff along with the other dates.
- The attendees list gained a multi-select RSVP status filter, and search results no longer show
  duplicates when a term matches multiple tags.
- **Recurring events are now DST-correct**: occurrences honor the recurrence rule's timezone, so
  a weekly 19:00 event stays at 19:00 across daylight-saving changes — existing non-UTC series
  were re-anchored automatically.
- The dashboard **Upcoming RSVPs** count now includes **Maybe** responses (previously Going
  only, hiding exactly the tentative RSVPs you're most likely to forget), with a URL-driven
  multi-select filter (All / Going / Maybe / Not going).

## 🌐 Embeds & Sharing

- **Embed Revel on your own website**: a filtered event list for your organization, a single
  event, or a series — via a one-line loader script or a plain iframe. Embeds honour
  `?theme=light|dark|auto`, resize to their content, and ship **no JavaScript** to your page.
- **oEmbed support**: pasting a Revel link into a supporting editor (many CMSs and forums)
  produces a live card automatically.
- Richer link previews: a versioned Open Graph share image, a square `og:logo` for chat unfurls,
  and Markdown no longer leaks into search-result and social-share descriptions.
- Invitation links got friendlier: the join pages now show *which* organization or event is
  inviting you (name, logo, tier), and an expired invitation is clearly distinguished from a
  used-up one (`410 Gone` with a machine-readable `reason`).

## 🗣️ Languages

- **Spanish and European Portuguese**: the whole interface — all ~7,900 strings — is now
  available in `es` and `pt`, matching the languages Revel already emails in. The SEO landing
  pages have editions too.
- Italian and German no longer fall back to English on ~480 strings each (account deletion,
  privacy settings, invitations, recurring events, waitlist messages…).
- Counted phrases now use each language's real plural forms — "3 persone si sono già unite"
  instead of an English "s" tacked onto a noun.
- German UI text is now gender-inclusive throughout (`Teilnehmer*innen`), and the profile
  language menu finally lists every available language (French had been missing).
- **Dates speak your language too**: every human-facing date now follows your chosen UI language
  with textual months everywhere, instead of browser-locale numeric dates — and first-time
  visitors no longer see the landing page flip from their language to English.

## 🎨 The Rebrand, Everywhere

- **"Bubble" + Nata Sans shipped for everyone**: lavender paper light mode, aubergine club dark
  mode, crimson heart accent, sticker-round corners, and the "let's revel." lockup — all color
  pairs pass WCAG AA contrast and color-blind separation checks.
- **Branded emails and PDFs**: every outbound email and generated document — invoices, credit
  notes, payout statements, tickets, revenue/VAT reports — restyled to the new brand. Tickets
  keep *your* organizer colors, adding only a subtle "Powered by let's revel." footer.
- Messages that were plain-text only (guest confirmations, invoice delivery, revenue reports)
  are now proper branded HTML.

## 🛠️ Under the Hood

- Backend runtime upgraded to **Python 3.14**; Django, anyio, httplib2, click, pyasn1, and
  pymdown-extensions all patched against known CVEs, and the frontend cleared a batch of
  high-severity dependency vulnerabilities.
- The staff admin panel got a major overhaul: ~30 list views optimized to stop per-row queries,
  richer filters and read-only audit fieldsets, and two admin-only security hardenings (escaped
  organizer-controlled names, superuser-only ownership transfer).
- Error responses across the API now have **honest, documented schemas** — what the OpenAPI spec
  says is what you actually get.
- GDPR data exports now use an explicit relation allowlist, so an export can never pick up
  third-party data.
- Search-engine crawl policy fixed: private areas (`/dashboard`, `/account`, org admin) are
  disallowed for *all* crawlers again, and AI-training bots stay blocked.
- **Financial documents can no longer be silently lost**: payout statements and attendee
  invoices record when their email was sent, surface delivery errors in the admin, and retry
  undelivered emails via a sweep; one failed payout email no longer aborts the rest of the batch.
- Telegram: banned users are blocked from the bot, and `/unsubscribe` now actually stops
  notifications.
- Self-hosting: a new interactive `bootstrap_admin` command (run by the setup wizard) creates
  the first superuser and organization on a fresh instance, and deploys now post a Discord
  notification when they go live.
- Ops: production now pages on Stripe money-correctness counters, deploys only roll out (and
  announce) components whose image actually changed, vulnerability-scanner probes are dropped at
  the Caddy edge, and public brand assets allow cross-origin embedding.

**📋 Full changelogs:** [Backend](https://github.com/letsrevel/revel-backend/blob/main/CHANGELOG.md) · [Frontend](https://github.com/letsrevel/revel-frontend/blob/main/CHANGELOG.md)
