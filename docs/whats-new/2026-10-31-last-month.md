# What's New — October 1 to October 31, 2026

A month of making Revel ready for Europe's paperwork. The **EU compliance package** adapts
ticketing and invoicing to each country's fiscal rules, and attendee invoices are now
**EN 16931 e-invoices** with embedded **Factur-X / ZUGFeRD** data. The engine behind them is
**[euinvoice](https://github.com/letsrevel/euinvoice)**, which we've released as an open-source Python library. Organizations can also run
Revel **on their own domain with their own brand colors**. New organizers land on a **launchpad**
that walks them to their first published event. And thanks to **CodeRabbit**, Revel is more
secure than it was a month ago.

## 🇪🇺 EU Compliance & E-invoicing

- **Country-aware compliance.** Revel now knows which features a country's law makes
  non-compliant, and blocks only that feature, only for the sales the law reaches, and only from
  the date it applies. Online card payment is refused for paid tickets at events held in Italy,
  where offline, bank-transfer and at-the-door sales keep working. Attendee invoices are turned
  off where a national real-time invoicing system applies (for example Croatia, Portugal and
  Romania, Basque TicketBAI, and Spain's Verifactu from 2027). Refusals name the country in your
  language, and the UI disables a blocked option up front (`compliance` capabilities on
  organization and event responses).
- **Organizer notices** flag duties that stay yours, such as cash-register rules in Austria and
  Denmark, Poland's fiscal register for some admission types, and a heads-up before the 2027
  Spanish change. They inform you without blocking anything.
- **Every ticket carries the fiscal basics.** Ticket PDFs and Apple / Google Wallet passes show
  the organizer's legal name and VAT ID (or tax number), a gap-free ticket number
  (`SERIES-000123`), the issue time, the price or "Free", and a "not a tax invoice" notice.
- **Skipped fiscal documents.** When a country rule means Revel must not issue an invoice or
  credit note, it's recorded so you can issue it from your own system. Owners get a filterable
  list with a resolve action for your external reference, a "Documents to issue yourself" sheet
  in the revenue report, and a daily digest.
- **EN 16931 e-invoices.** Attendee invoices and credit notes now carry everything structured
  e-invoicing needs:
  - structured postal addresses for organizations, billing profiles and business buyers, with
    the country prefilled from the VAT ID;
  - a VAT category per line with legal exemption reasons for 0% VAT, which tiers inherit from
    an organization default;
  - the supply date and the payment method;
  - EN 16931 totals that preserve the charged amount to the cent.
- **Factur-X / ZUGFeRD PDFs.** Compliant invoices and credit notes are issued as PDF/A-3 with the
  machine-readable CII XML embedded. Any accounting system that reads Factur-X picks them up
  without retyping. Every generated document is validated against the official CEN rules in CI,
  and the feature has a site-wide switch (`einvoice_facturx_enabled`).
- **Small-business organizers** (German Kleinunternehmer, Austrian Kleinunternehmerregelung) can
  now turn on attendee invoicing. Every line is VAT-exempt with the statutory note, and tickets
  show the tax number when there's no VAT ID.
- Credit notes now mirror the original invoice's lines exactly, and partial refunds are prorated
  so they add up to the invoiced amount to the cent. Revenue reports split plain 0% sales from
  reverse-charge ones.

### 📦 [euinvoice](https://github.com/letsrevel/euinvoice): our e-invoicing engine, now open source

Everything above runs on **[euinvoice](https://github.com/letsrevel/euinvoice)**, a small, typed,
**MIT-licensed** Python library we built and published on [PyPI](https://pypi.org/project/euinvoice/) (`pip install euinvoice`):

- one EN 16931 model, written out as **UBL** or **CII**;
- **Peppol BIS 3.0**, **XRechnung 3.0**, **Factur-X / ZUGFeRD** (generate and embed in PDF/A-3),
  and Italian **FatturaPA**;
- validation against the **official** XSD and Schematron rule sets (CEN, Peppol, KoSIT), not a
  reimplementation;
- framework-free: bytes in, bytes out, with no Django, network or file-system access in the core.

It's pre-1.0 (currently 0.2.1). If you work with European e-invoices in Python, try it, file
[issues](https://github.com/letsrevel/euinvoice/issues) and send PRs. Docs: [letsrevel.github.io/euinvoice](https://letsrevel.github.io/euinvoice/).

## 🎨 Your Brand, Your Domain

- **Custom domains (white-label).** An organization can serve its public site on its own domain,
  for example `events.example.org`:
  - the organization page becomes the home page, with your name, logo and colors in the header
    and footer, and a small "Powered by Revel" line;
  - sign-up, RSVPs, paid checkout, seat holds, memberships and the attendee dashboard all work
    end to end on the domain;
  - attendee emails link to it, and old Revel URLs of your pages redirect there.

  Owners connect a hostname from **Admin → Custom domain**, see the exact DNS records to add,
  get hints for the usual DNS mistakes, and watch the live status. The feature is enabled per
  organization on request.
- **Brand colors.** Pick a primary and an optional accent color, and organization, event, series,
  membership and embed pages use them in light and dark mode. Revel adjusts lightness where
  needed so text stays readable (WCAG contrast), and the settings show a live preview.
- Your **logo and colors** also appear on wallet passes, ticket PDFs and the footer of
  organization emails.

## 🚀 Getting Started & Running Your Org

- **Organization launchpad.** Right after creating an organization, owners land on a welcome page
  with two next steps side by side: create your first event, and connect Stripe to get paid. A
  setup card on the dashboard tracks the five steps until your first event is live. Creating an
  organization now asks only for a name and a contact email.
- **Redesigned admin dashboard.** A "Next up" panel shows the next or current event with
  attendance and quick links to check-in and the guest list. "Needs attention" counts pending
  requests, drafts and missing billing details. The admin tabs are now a grouped sidebar.
- **Redesigned event editor.** The title and status sit on top, with tabs right below. Details are
  split into clear sections with a live summary rail and a single Save button. New events start
  by asking "RSVP or Tickets?". Descriptions can be edited full-screen.
- **Publish and share.** Drafts show "Ready to go live?" with a Publish button. Right after
  publishing you get the link to copy plus WhatsApp, Telegram and email share buttons.
- **Connect Stripe without losing your place.** Choosing online payment on a paid tier lets
  owners connect Stripe and come straight back to the tier they were building.
- **Setup nudges.** Owners of organizations that stall (a private page, forgotten drafts, no
  events yet) get a few gentle, capped reminders. You can opt out of them like any other
  notification.

## 🎟️ Events & Tickets

- A tier's availability now reflects the event's capacity as well: a 40-ticket tier on an event
  capped at 30 shows 30, and the tier form warns when a tier exceeds the cap.
- **Free tickets can be self-cancelled.** New free tiers let attendees cancel by default, which
  frees the spot for someone else, and the cancel dialog no longer talks about refunds.
- **Card disputes** are tracked per payment. Staff with ticket permissions are notified when a
  dispute opens, with the amount, reason and evidence deadline, and again when it closes.
- Checkout prefills the ticket-holder name when there's a single ticket. Claimed invitation links
  now move you straight to the next step instead of asking you to claim again.
- Cover art shows uncropped at 16:9, and the "Part of a series" card shows the series logo and
  description.

## 🔔 Notifications & Comms

- **Organizer alerts have their own settings.** Ticket sold, cancelled and refunded, and series
  pass sold and cancelled, are now separate staff notification types. Your own receipts stay
  "Always on", and settings explain which emails can't be turned off.
- **"Unsubscribe" in your mail app now reaches Revel**, and turns off only that one notification
  type. Receipts and account emails can't be silenced by accident.
- Emails, notification dates and **Apple Wallet passes** now follow each recipient's language,
  for example „Montag, 26. Oktober 2026, 13:20 CET". Guest confirmations are sent in the
  language the guest used on the site.

## 🌍 Self-hosting & Infrastructure

- **Custom domains for self-hosters.** The infra stack ships a Caddy on-demand TLS catch-all
  backed by `GET /api/tls/ask`, and a tenant mode for the frontend. Setup is documented in the
  white-label operator guide.
- **Bot check on registration** with Cloudflare Turnstile (`TURNSTILE_SITE_KEY` /
  `TURNSTILE_SECRET_KEY`). It's off by default, and sign-ups stay open if Cloudflare is
  unreachable.
- `PUBLIC_HIDE_SOLUTIONS_FOOTER` hides the marketing footer links on self-hosted instances, and a
  configurable support email (`support_email` on `/version`) feeds the "contact us" buttons.
- Grafana moves to 13.2, and the setup wizard covers the new environment variables.
- `/llms.txt` gives AI assistants a plain-text summary of Revel, and `robots.txt` now lets AI
  search assistants in while keeping training crawlers off event and organization pages.

## 🛡️ Security, and a thank-you to CodeRabbit

A big **thank you to [CodeRabbit](https://www.coderabbit.ai/)** for their open-source security
pledge, and for offering **free deep security scans to open-source projects**. Revel is open
source end to end, and their scan, alongside the CodeRabbit reviews that already check every
pull request we open, helped us make the platform more secure across the backend, the frontend
and our infrastructure. If you maintain an open-source project, take them up on it.

This month also brought the usual upkeep. Outbound connections stopped trusting a certificate
authority that was distrusted in October, and dependencies were patched as advisories were
published.

---

**📋 Full changelogs:** [Backend](https://github.com/letsrevel/revel-backend/blob/main/CHANGELOG.md) · [Frontend](https://github.com/letsrevel/revel-frontend/blob/main/CHANGELOG.md)
