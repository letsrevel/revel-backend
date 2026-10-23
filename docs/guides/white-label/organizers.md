# White-Label for Organizers

This guide covers the two things an organization can customize: its **brand colors**, which every
organization can set, and its **custom domain**, which the Revel platform enables per organization.
It also explains what changes for your attendees. For what is deliberately not customizable (the
Revel footer, shared accounts, email sender), see the [overview](index.md#the-deliberate-limits).

## Brand colors

Brand colors are free for every organization. Open your organization's admin settings and find
**Brand colors**. You can set:

- **Primary color**: used for buttons, links and highlights.
- **Accent color** (optional): used for small touches such as the follow heart. An accent only
  takes effect together with a primary color.

Enter colors as six-digit hex values, `#RRGGBB`, for example `#8C3CDD`. Upper or lower case both
work. Leave both empty, or use **Reset to Revel defaults**, to go back to the standard Revel look.
You need the "edit organization" permission to change them.

### Readability adjustment

Revel pages come in a light and a dark mode, and one color rarely reads well on both. A pale yellow
is fine on a dark page and almost invisible on a light one. Instead of rejecting such colors, Revel
accepts any valid color and adjusts it **per mode** so text and buttons stay legible.

For each mode, Revel keeps your color's hue and saturation and nudges its lightness (darker in
light mode, lighter in dark mode) in 1% steps, stopping at the first step that is readable on that
mode's page background **and** on cards. A color that is already readable is used exactly as you
entered it. The text on top of a button (black or white) is then chosen for the best contrast.

The targets are WCAG AA contrast ratios: 4.5:1 for the primary color, because it is also used as
link text, and 3:1 for the accent, which is used for graphics and small UI elements.

| You enter | Light mode uses | Dark mode uses | Why |
|---|---|---|---|
| `#8C3CDD` (Revel purple) | `#8C3CDD`, white text | `#A667E5`, black text | Readable on light as entered; lightened for dark |
| `#FFFF00` (yellow) | `#707000`, white text | `#FFFF00`, black text | Far too bright for a light page; fine on dark |
| `#FEFEFE` (near white) | `#6D6D6D`, white text | `#FEFEFE`, black text | The same effect, taken to the extreme |
| `#0A0A0A` (near black) | `#0A0A0A`, white text | `#878787`, black text | The mirror image |

Accent colors follow the same rule with the lower 3:1 target. For example an accent `#F9B233`
becomes `#C07D06` in light mode and stays `#F9B233` in dark mode.

### Live preview

The settings page shows a live preview of buttons, links and badges in both light and dark mode
while you type, and tells you for each mode whether your color is **used as entered** or **adjusted
for readability**. Nothing is saved until you press **Save colors**. The preview uses the same
calculation the real pages use, so what you see is what visitors get.

### Where your colors appear

| Surface | What changes |
|---|---|
| Public pages | Your organization page, event pages, series pages, membership and resources pages and questionnaires use your colors. On a [custom domain](#custom-domain) they apply to the whole site, including login, account and legal pages. |
| Embeds | Embedded event lists and widgets use your colors. |
| Emails | Emails that belong to your organization show your **logo** in the signature. Email colors stay Revel's. |
| Wallet passes | Apple Wallet and Google Wallet cards use your primary color as the card background, with black or white text. Passes use your color **as entered**, not the readability-adjusted one, because a card is a solid surface and not text on a page. Your accent is not used on passes. |
| PDFs | Ticket, series pass and membership card PDFs use your primary color for the banner, tier badges and frames. Small text and thin lines use the darker, readability-adjusted variant so they stay legible on the paper. |

A few details worth knowing:

- Changing your colors refreshes PDFs and Apple passes the next time they are generated. A card a
  guest **already saved to Google Wallet** keeps the look it had when it was saved. New saves use
  the new colors.
- The "Powered by revel." mark on PDFs and passes is not recolored.
- Your logo is used in emails, wallet passes and the tenant header. Upload a square image
  (400x400 pixels is a good size).

## Custom domain

A custom domain serves your whole attendee journey from your own address, for example
`events.example.org`: your organization page, events, applications, memberships and checkout.
Visitors there see your name, logo and colors, and only your events.

### Before you start

- **Ask the platform to enable it.** Custom domains are switched on per organization by a Revel
  platform admin. Until then, requesting a domain answers "Custom domains are not enabled for this
  organization".
- **Only the organization owner** can request, check or remove the domain.
- **Choose a subdomain** such as `events.example.org` or `tickets.example.org`. An address with no
  subdomain (`example.org`) usually cannot hold a CNAME record at DNS providers.
- You need access to the DNS settings of the domain.

A hostname must be a full domain name with at least one dot, without `https://`, a port, a wildcard
(`*.example.org`) or an IP address. It cannot be a Revel hostname or a subdomain of one. The same
hostname cannot be used by two organizations at once.

### Request flow

1. In your organization settings, open **Custom domain** and enter the hostname.
2. Revel answers immediately with the **DNS records to create**. The domain is now **Pending**.
3. Create the records at your DNS provider (see below).
4. Wait for the domain to turn **Active**, or press **Check now**.

Only one domain can be pending or active at a time.

### DNS records

You always create one record, and sometimes more.

| Type | Name | Value | When |
|---|---|---|---|
| `CNAME` | your hostname, for example `events.example.org` | the target shown on the page (for example `customers.letsrevel.io`) | Always, when the platform has configured a target |
| `TXT` | the name shown | the value shown | Only when the page lists TXT records, for hostname ownership or certificate validation |

Create exactly the records the page shows, with exactly the values it shows. The list can be
refreshed while the domain is pending, so if a TXT record appears later, add it. DNS providers
differ on whether the name field wants the full hostname or only the part before your domain. If
validation does not move, that is the first thing to check.

Revel asks Cloudflare to validate the hostname over HTTP and to issue the certificate
automatically, so you do not need to buy or upload a certificate.

### Statuses

| Status | Meaning | What to do |
|---|---|---|
| **Pending** | Requested. Waiting for your DNS records and for the certificate. The hostname does not work yet. | Create the records, then wait or press **Check now**. |
| **Active** | DNS is verified and the certificate is issued. Your site is live on the domain. | Nothing. If the page shows a warning here, see below. |
| **Failed** | The domain could not be verified or provisioned. Your site is not served from it. | Read the errors, fix the cause, and request the domain again. |

Removed domains disappear from the page. A removed hostname is free again.

### Timing and "Check now"

DNS changes can take anywhere from a few minutes to a few hours to be visible, depending on your
provider. Once they are, Cloudflare validates the hostname and issues the certificate, usually
within minutes. Revel checks every pending domain on its own **every 15 minutes**, so a domain
normally turns active within about 15 minutes of Cloudflare being ready, without you doing anything.

**Check now** asks Cloudflare for the current state immediately and updates the page, so you do
not have to wait for the next scheduled check. It reports what Cloudflare says; it does not make
Cloudflare validate any faster. If DNS has not propagated yet, pressing it repeatedly changes
nothing.

### Troubleshooting

The page shows the problems Cloudflare reported at the last check, as plain messages. Typical
causes:

| What you see | Likely cause | Fix |
|---|---|---|
| Stays **Pending** for hours, no error | The CNAME is missing, has the wrong name, or points somewhere else. A TXT record is missing. | Compare your DNS records to the page character by character. Check the CNAME with `dig +short events.example.org CNAME`. |
| A message that the hostname "moved" or no longer points to the fallback origin | Cloudflare gave up waiting for the CNAME, or the CNAME was changed. | Restore the CNAME. A pending domain that hits this becomes **Failed**: request it again. On an **Active** domain the message is recorded as a warning and the domain stays active, so restoring the record is enough. |
| A certificate status like `validation_timed_out` | Certificate validation did not finish in time. | Fix DNS, then request the domain again if it turned **Failed**. On an **Active** domain it is a warning. |
| **Failed** with a blocked or deleted hostname | Cloudflare refused or removed the hostname. | Request it again. If it fails again, contact Revel support with the hostname. |
| "This hostname is already in use." | Another organization has it (pending or active). | Use a different hostname, or contact support if it is yours. |
| "This organization already has a pending or active domain." | You can only have one live domain. | Remove the current one first. |
| "This hostname belongs to the platform and cannot be used." | The hostname is a Revel address or a subdomain of one. | Use your own domain. |
| "We could not reach the domain provisioning service" (HTTP 502) | Cloudflare could not be reached when you asked. | Try again in a few minutes. Nothing was changed. |

### Removing or re-requesting a domain

Use **Remove** on the custom domain page. Revel deletes the hostname from Cloudflare and frees it,
and your attendee pages go back to the Revel address. Browsers and the platform may cache the old
redirect for a short while (the platform forgets a domain within about a minute, and permanent
redirects to the custom domain are cached by browsers for up to five minutes).

A **Failed** domain does not need to be removed first. Request the same or a different hostname
again and Revel retires the failed one for you. A removed domain cannot be brought back, but you
can request the same hostname again as a new request.

!!! note "Remove the DNS records too"
    Removing a domain in Revel does not touch your DNS provider. Delete the CNAME (and TXT)
    records you created when you no longer need them.

## What attendees experience

**Their own login per domain.** Browsers keep sessions per address, so someone signed in to Revel
who opens `events.example.org` is asked to log in there. It is the same Revel account (same email
and password, same data) and each domain simply has its own session. Single sign-on buttons are not
shown on custom domains.

**Links go to your domain.** Once the domain is active, links in attendee emails (ticket, payment
confirmation, invitation, event updates and reminders) point at it. Visiting your organization,
event or series on the Revel address redirects to the same page on your domain. Your organization
admin pages stay on Revel.

**Paying.** Checkout is Stripe's hosted page, branded by your own Stripe account. When the attendee
finishes or cancels, Stripe sends them to a Revel address, which forwards them straight back to the
same event on your domain with their payment result. They may notice the Revel address for a
moment. They are never left on it.

**Seated events and guests.** If your event has a seat map, guests (people without a Revel account)
can pick and hold seats, check out and confirm by email on your custom domain, exactly as on the
Revel address.

### Account emails and why they link to Revel

Emails that carry a login-sensitive link always go to **Revel's own site**, not your domain. This
covers email verification, password reset, email change confirmation, account deletion
confirmation and unsubscribe links. When someone signs up or resets a password from your domain,
the email they get links to Revel, and after they confirm, Revel takes them back to your
organization's page.

The reason is security. Those links contain a single-use token that works for the person's Revel
account on **every** organization, not only yours. You control your domain's DNS, so you could in
principle point it at any server. If the token links led to your domain, anyone who controlled a
custom domain could trigger a password reset for any Revel user and collect the token. Keeping
these links on Revel closes that door for every organization, including yours. The email's
branding (organization name and logo) can still be yours. Only the link target is fixed.

One deliberate exception: the confirmation link in a **guest** RSVP or ticket email points at your
domain. That token is single-use, scoped to one RSVP or ticket on your own event and gives no
login session.

## For developers integrating with the API

| Action | Endpoint |
|---|---|
| Replace branding | `PUT /api/organization-admin/{slug}/branding` (body `{"primary_color": "#RRGGBB", "accent_color": null}`; full replace, `{}` clears). The current value is the `branding` field of the organization payloads, with the derived `brand_theme` next to it |
| Preview derived colors | `GET /api/organization-admin/{slug}/brand-theme-preview?primary=%23RRGGBB&accent=%23RRGGBB` |
| Read the current custom domain | `GET /api/organization-admin/{slug}/custom-domain` |
| Request a domain | `POST /api/organization-admin/{slug}/custom-domain` with `{"hostname": "events.example.org"}` |
| Check now | `POST /api/organization-admin/{slug}/custom-domain/refresh` |
| Remove | `DELETE /api/organization-admin/{slug}/custom-domain` |

The brand endpoints need the `edit_organization` permission. The custom-domain endpoints are
owner-only and do not accept third-party app tokens. See the generated API docs for the full
schemas.
