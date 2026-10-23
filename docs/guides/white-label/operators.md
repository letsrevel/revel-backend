# White-Label for Platform Operators

This guide is for whoever runs a Revel instance (the hosted platform or a self-hosted one) and
wants to let organizations serve their attendee journey from their own domains. For what
organizers see, read the [organizer guide](organizers.md). For why the pieces are built the way
they are, see the [architecture page](../../architecture/white-label.md).

Brand colors need no operator work: every organization can set them. Everything below is about
**custom domains**.

## The moving parts

A custom domain touches four layers, and all four must agree before a customer goes live.

| Layer | Job | Configured by |
|---|---|---|
| DNS and TLS | Send the customer's hostname to your infrastructure and present a valid certificate for it | Cloudflare for SaaS, or your own TLS (see [without Cloudflare](#running-without-cloudflare)) |
| Reverse proxy | Accept any custom hostname and forward the original host to the frontend | Caddy |
| Frontend | Turn the incoming host into an organization (tenant mode) | Frontend environment variables |
| Backend | Own the domain lifecycle, resolve hosts, allow CORS, build links | Backend environment variables, Django admin |

## Enabling custom domains for an organization

Each organization has a platform flag, `custom_domain_enabled`. It defaults to **off**, appears
only in the Django admin (never in an API edit schema) and can be changed only by platform staff
with admin access. Brand colors do not need it.

1. In the Django admin open **Organizations**, pick the organization, and in the **Custom domain**
   section tick `custom_domain_enabled`. The `custom_domain` field below it is read-only: it holds
   the hostname of the active domain and is maintained by the domain service.
2. The organization's owner can now request a domain (see the [organizer
   guide](organizers.md#custom-domain)).

The flag is also the **kill switch**. Turning it off for an organization, at any time:

- stops its hostname resolving (the by-domain lookup answers 404, so the frontend serves a 404 on
  that host),
- stops dynamic CORS for that host,
- withdraws `custom_domain` from API payloads, so links in emails and redirects fall back to the
  platform URL,
- returns 403 on the owner's custom-domain endpoints and skips the organization in the sync task,
- sends tenant mail from the platform sender again.

The stored hostname and the domain row are kept, so turning the flag back on restores the domain
without a new request. Saving the flag in the admin drops the cached lookup for that host
immediately. The frontend caches lookups for up to 60 seconds per replica, so allow about a minute
for everything to agree.

## Cloudflare for SaaS

Revel provisions customer hostnames through [Cloudflare for
SaaS](https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/) (Custom
Hostnames) on **your** zone. Customers point a CNAME at a **fallback origin** you control, Cloudflare
validates the hostname, issues and renews the certificate, and proxies traffic to your origin.

### Setup

1. **Enable Custom Hostnames** on the zone that holds your platform domain (SSL/TLS, Custom
   Hostnames). Cloudflare for SaaS may require a plan or add-on on your account.
2. **Create the fallback origin.** Add a DNS record such as `customers.<your-domain>` that points
   at your origin server, and set it up like your other hosts: first **DNS only** (grey cloud) so
   Caddy can obtain its certificate for it, confirm it serves HTTPS, then switch it to **Proxied**
   (orange cloud). This is the same caveat described in [DNS &
   Cloudflare](../../self-hosting/dns-cloudflare.md#the-cloudflare-orange-cloud-caveat). Then
   register that hostname as the **fallback origin** in the Custom Hostnames settings.
3. **Create an API token** scoped to that zone with the permission **SSL and Certificates: Edit**.
   Revel only creates, reads and deletes custom hostnames, so nothing broader is needed. Note the
   **Zone ID** from the zone's overview page.
4. **Set the backend variables** (below): the token, the zone id, and the fallback origin hostname
   as the CNAME target. Restart the web and Celery containers.
5. **Decide how the origin sees TLS.** With the zone on **Full (strict)**, Cloudflare connects to
   your origin and verifies its certificate. For custom hostnames it can present the *customer's*
   hostname as the TLS server name (SNI), for which Caddy has no certificate. Either configure
   Cloudflare to send the fallback hostname as the SNI (the custom origin SNI option), so the
   certificate Caddy already holds for the fallback hostname matches, or install a Cloudflare
   Origin CA certificate on the catch-all block. Check with a test domain before the first
   customer. Revel does not set an origin SNI when it creates hostnames.

When an organization requests a domain, Revel calls Cloudflare to create the custom hostname with
**HTTP** domain-control validation and a DV certificate, so the customer normally needs only the
CNAME. If Cloudflare also asks for TXT records (hostname ownership, certificate validation), Revel
shows them to the organizer alongside the CNAME.

Provisioning is on only when **both** `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` are set.

## Environment variables

### Backend

| Variable | Default | Meaning |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | empty | API token with *SSL and Certificates: Edit* on the zone. |
| `CLOUDFLARE_ZONE_ID` | empty | Zone that owns the custom hostnames. Provisioning is enabled only when this **and** the token are set. Neither alone does anything. |
| `CUSTOM_DOMAIN_CNAME_TARGET` | empty | The fallback origin hostname organizers CNAME to, for example `customers.example.com`. A bare hostname: no scheme, path, port or spaces. It is lowercased and a trailing dot is removed. Shown to organizers as the CNAME value. |
| `CUSTOM_DOMAIN_URL_TEMPLATE` | `https://{hostname}` | Origin of an organization's active domain, used for links in emails and notifications. Must contain `{hostname}` and start with `http://` or `https://`. Local development and E2E use `http://{hostname}:5173`. |

Related variables that matter for white-label: `BASE_URL` and `FRONTEND_BASE_URL` (a custom
hostname can never equal, or sit under, the hosts and registrable apex of these two),
`ORG_EMAIL_DOMAIN` (see [email rollout](#email-rollout-order)) and `TURNSTILE_SITE_KEY` /
`TURNSTILE_SECRET_KEY` (see [Turnstile](#turnstile)). The API's own `ALLOWED_HOSTS` and
`CORS_ALLOWED_ORIGINS` do **not** need to list customer domains: the browser talks to the API host,
and CORS for active custom domains is allowed dynamically.

!!! warning "Two-label apex detection"
    A hostname is rejected when it equals or sits under the platform's hosts or their apex, and
    the apex is taken as the last two labels. If your platform runs under a multi-part public
    suffix (for example `app.example.co.uk`), the apex becomes `co.uk` and **every** `*.co.uk`
    custom domain is refused. Hosted on `.io`, `.org`, `.com` and similar this never matters.

### Frontend

The frontend reads these at **runtime** (no rebuild). The full reference, including Kubernetes
examples, is in the frontend [deployment
runbook](https://github.com/letsrevel/revel-frontend/blob/main/docs/runbooks/deployment.md) and
[Tenant mode](https://github.com/letsrevel/revel-frontend/blob/main/docs/tenant-mode.md#environment-variables).

| Variable | Default | Meaning |
|---|---|---|
| `PLATFORM_HOSTS` | unset (tenant mode **off**) | Comma-separated platform hostnames, no ports, case-insensitive. Any other host is treated as a possible custom domain. Unset means nothing redirects, rewrites links or scopes routes, even if the API returns a `custom_domain`. `localhost`, `127.0.0.1` and `::1` are always platform hosts, so container health checks keep working. |
| `PLATFORM_ORIGIN` | `https://` plus the first `PLATFORM_HOSTS` entry | Where tenant hosts send admin, create-organization and OAuth routes; also the target of the "Powered by Revel" link. |
| `PUBLIC_CUSTOM_DOMAIN_URL_TEMPLATE` | `https://{hostname}` | Origin template for absolute custom-domain links, redirects and SEO canonicals. Must start with `http(s)://` and contain `{hostname}`. A public variable (also read in the browser). |
| `HOST_HEADER` | unset | Header carrying the public host, normally `x-forwarded-host`. Replaces `ORIGIN` on multi-host deployments. |
| `PROTOCOL_HEADER` | unset | Header carrying the public scheme, normally `x-forwarded-proto` (https when absent). |
| `ORIGIN` | unset | Pins a single public origin. **Do not set it on a multi-host deployment.** It wins over the two headers and pins every host to the platform, which breaks form posts on custom domains. |

!!! danger "The two URL templates must match"
    The backend `CUSTOM_DOMAIN_URL_TEMPLATE` and the frontend `PUBLIC_CUSTOM_DOMAIN_URL_TEMPLATE`
    must be identical. The backend builds the links in emails and notifications, and the frontend
    builds redirects and canonicals. If they differ, the two sides disagree about where an
    organization lives. Change them together.

A minimal production setup for a platform on `revel.example.com` with a fallback origin
`customers.example.com`:

```bash
# backend .env
BASE_URL=https://api.example.com
FRONTEND_BASE_URL=https://revel.example.com
CLOUDFLARE_API_TOKEN=...
CLOUDFLARE_ZONE_ID=...
CUSTOM_DOMAIN_CNAME_TARGET=customers.example.com
CUSTOM_DOMAIN_URL_TEMPLATE=https://{hostname}

# frontend environment (no ORIGIN)
PUBLIC_API_URL=https://api.example.com
HOST_HEADER=x-forwarded-host
PROTOCOL_HEADER=x-forwarded-proto
PLATFORM_HOSTS=revel.example.com
PLATFORM_ORIGIN=https://revel.example.com
PUBLIC_CUSTOM_DOMAIN_URL_TEMPLATE=https://{hostname}
```

## Caddy

The reverse proxy needs a block that accepts **any** hostname that reaches it for a custom domain
and proxies to the frontend. The requirements, which the catch-all block in your Caddyfile must
meet (the work is tracked in [letsrevel/infra#67](https://github.com/letsrevel/infra/issues/67);
check your Caddyfile for it):

- Proxy to the frontend with the same headers as the main frontend block, and forward the original
  `Host` as `X-Forwarded-Host` and the scheme as `X-Forwarded-Proto`, **overwriting** any values a
  client sends. The frontend trusts these to decide which organization it is serving.
- Terminate TLS with a certificate that satisfies Cloudflare's origin connection (see [step 5 of the
  setup](#setup)).
- Send HSTS **without** `includeSubDomains` for foreign domains. They are not yours.
- Never buffer SSR streaming responses, as for the main frontend block.
- Protected media is served by Caddy with a CORS header pinned to the platform frontend domain.
  Customer origins need the allowed origin echoed back, or media behind HMAC links will not load
  on custom domains. This is part of the same infra work.

## Turnstile

If you use Cloudflare Turnstile on registration (`TURNSTILE_SITE_KEY` and `TURNSTILE_SECRET_KEY`
both set), the widget only renders on hostnames in its hostname list in the Cloudflare dashboard.
The backend does not check the hostname, so there is nothing to configure in Revel: **add each
custom domain to the widget's hostname list** when you activate it, or sign-up on that domain
fails. Cloudflare caps the number of hostnames per widget on the free plan; check the current limit
when you plan how many domains to host, and see whether a second widget or a plan change fits.
Self-hosters who leave both keys empty have no Turnstile and nothing to do.

## The sync task

`events.sync_custom_domains` is a Celery beat task, registered by a data migration as "Sync pending
and active custom domains from Cloudflare", every **15 minutes** and enabled by default. The
migration uses `get_or_create`, so if you disable the task in the admin it stays disabled across
deploys.

Each run reads every domain that is **pending or active**, has a Cloudflare id and whose
organization has `custom_domain_enabled` on, fetches its state from Cloudflare and applies the
result:

| Cloudflare reports | Pending domain | Active domain |
|---|---|---|
| Hostname **and** certificate `active` | becomes **active** | stays active |
| Hostname `blocked` or `deleted`, or certificate `deleted` | becomes **failed** | becomes **failed** and is withdrawn from the organization |
| Hostname `moved` (validation exhausted, or the CNAME no longer points to the fallback origin) | becomes **failed** | stays **active**; the problem is recorded and shown to the organizer |
| A certificate `*_timed_out` status | becomes **failed** | stays **active**; the problem is recorded |
| Anything else (for example `active_redeploying`) | unchanged | unchanged |

`moved` is treated this way because it is recoverable: a customer who briefly breaks their CNAME
should not lose a working site. Cloudflare keeps the hostname for a while and reports `deleted` if
it finally gives up, which then fails the domain. DNS records and error messages are refreshed on
every run either way.

The decision is a compare-and-set: before applying a transition the task re-reads the status under
a row lock, and does nothing if the organizer removed the domain in the meantime. One domain
raising an error is logged (`custom_domain_sync_failed`, with the domain id) and does not stop the
rest. The task returns `{"synced": n, "failed": n}` and is a no-op when Cloudflare is not
configured. The organizer's **Check now** runs the same sync for their own domain on demand.

## Manual activation in the Django admin

Staff can manage domains under **Custom Domains** in the admin sidebar (the
`OrganizationDomain` model). Select rows and run an action:

- **Activate selected domains**: marks the domain active and publishes its hostname on the
  organization. Fails for an organization with `custom_domain_enabled` off, for a removed domain,
  and when the organization already has another live domain or the hostname is taken.
- **Mark selected domains as failed**: withdraws the hostname from the organization.
- **Remove selected domains**: deletes the Cloudflare hostname (when provisioned) and frees the
  hostname for other organizations.

The status field is read-only and deleting rows is disabled, because status changes must go through
these actions: they keep the organization's `custom_domain` column and the lookup cache in step.
Use the actions rather than editing the database. You can also add a domain row for an
organization here. This is how domains are created without Cloudflare.

## Running without Cloudflare

Cloudflare is optional. Leave `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` empty and Revel
runs in **manual mode**:

- A domain request only validates the hostname and records a **pending** row with the CNAME
  instruction. Nothing is provisioned, the sync task does nothing and the organizer's **Check now**
  has nothing to check.
- A platform admin **activates** the domain in the Django admin once DNS and TLS are ready.
- **TLS is yours.** You need a certificate for each hostname on your own proxy. The simplest route
  is to add the hostname as a site block in your Caddyfile after the organizer creates the
  CNAME (Caddy then obtains a Let's Encrypt certificate). Caddy's on-demand TLS also works, but
  it needs an "ask" endpoint that answers 200 only for hostnames you serve. Revel does not ship
  one: the by-domain endpoint takes the hostname in the URL path, while Caddy's ask request puts
  it in a `?domain=` query, so you would need a small shim in front of it.
- Set `CUSTOM_DOMAIN_CNAME_TARGET` to a hostname that resolves to your server (for example
  `customers.example.com` with an A record). It is the only thing organizers are told to create,
  and the instructions are empty without it.
- Everything else (frontend env vars, Caddy forwarding headers, templates, checks, email) is
  identical to the Cloudflare setup.

Self-hosters who run a single organization do not need any of this: point `FRONTEND_DOMAIN` at
that organization's domain and use [`FEATURE_ORGANIZATION_CREATION=False`](../../self-hosting/tiers.md#single-org-instances).

## Deploy-time checks

`manage.py check` (which the web container runs through `migrate` on start) validates the
white-label configuration. IDs are verified against the code.

| Check | Level | When |
|---|---|---|
| `events.W001` | Warning | `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` are set but `CUSTOM_DOMAIN_CNAME_TARGET` is empty, so organizers are not told which CNAME to create |
| `events.W002` | Warning | `CUSTOM_DOMAIN_CNAME_TARGET` is set but is not a bare hostname (a scheme, path, port or spaces would be shown to organizers as the CNAME value). Fires with or without Cloudflare |
| `common.E002` | **Error**: `check` and `migrate` fail | `CUSTOM_DOMAIN_URL_TEMPLATE` has no `{hostname}`, has stray braces or other placeholders, or does not start with `http://` or `https://`. Applies even with `DEBUG` on |
| `common.W005` | Warning | `CUSTOM_DOMAIN_URL_TEMPLATE` is not `https` while `DEBUG` is off |

Celery workers and beat do not run these checks. The table of all deploy-time checks is in
[Tiers & Configuration](../../self-hosting/tiers.md#domains).

## Email rollout order

Tenant organizations send attendee emails (tickets, payment confirmations, memberships, waitlist,
subscriptions) through the **organization sender**,
`"<Org> via Revel" <org-slug@ORG_EMAIL_DOMAIN>`, instead of the platform sender. This works in
two states, and the order you roll them out matters.

| State | Sender domain | Consequence |
|---|---|---|
| `ORG_EMAIL_DOMAIN` **unset** | The domain of `DEFAULT_FROM_EMAIL`, for example `example.com` | Authenticated (it is the domain you already send from), but tenant ticket and payment mail **shares the apex reputation with account mail** (verification, password reset). |
| `ORG_EMAIL_DOMAIN` **set** (for example `mail.example.com`) | The dedicated subdomain | Mandatory ticket and payment mail now **shares the `mail.` subdomain with organization bulk mail** (announcements, reminders, invitations). A complaint spike on one organization's bulk mail can hurt the deliverability of every tenant's ticket mail. |

**Recommended order:** complete the sending-domain rollout (the `ORG_EMAIL_DOMAIN` setup, DKIM and
DMARC authentication, [DNS → Sending domain](../../self-hosting/dns-cloudflare.md#sending-domain-for-organization-mail),
tracked for the hosted platform in [letsrevel/infra#57](https://github.com/letsrevel/infra/issues/57))
**before** enabling custom domains for a customer. That way tenant mail starts on its final sender
domain, and you never move a customer's sender identity mid-flight. Watch spam rates for the sending
domain in Google Postmaster Tools. Gmail does not forward complaints to the webhook.

Account emails (verification, password reset, deletion) and unsubscribe links stay on the platform
sender and the platform URL, never on a customer domain. See [Email Sending &
Opt-Out](../../architecture/email-sending.md#sender-identity).

## Go-live checklist

Once per instance:

- [ ] Backend `BASE_URL` and `FRONTEND_BASE_URL` are public origins, and `manage.py check` is clean
      (no `events.W001`, `events.W002`, `common.E002`, `common.W005`).
- [ ] Cloudflare for SaaS is enabled, the fallback origin hostname exists, is proxied and serves
      HTTPS, and is registered as the fallback origin.
- [ ] `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID` and `CUSTOM_DOMAIN_CNAME_TARGET` are set on web
      and Celery, and the services are restarted.
- [ ] Origin TLS under Full (strict) is verified with a test hostname (SNI or Origin CA decision made).
- [ ] The Caddy catch-all block forwards `X-Forwarded-Host` and `X-Forwarded-Proto`, sends no
      `includeSubDomains` HSTS, does not buffer SSR streams, and handles protected-media CORS.
- [ ] The frontend runs with `HOST_HEADER`, `PROTOCOL_HEADER`, `PLATFORM_HOSTS`, `PLATFORM_ORIGIN`
      and `PUBLIC_CUSTOM_DOMAIN_URL_TEMPLATE`, and **without** `ORIGIN`.
- [ ] The backend `CUSTOM_DOMAIN_URL_TEMPLATE` and the frontend `PUBLIC_CUSTOM_DOMAIN_URL_TEMPLATE`
      are identical.
- [ ] The `events.sync_custom_domains` periodic task is enabled in the admin (it is by default).
- [ ] `ORG_EMAIL_DOMAIN` is set and authenticated (DKIM, DMARC), or you have accepted the apex
      reputation trade-off described above.
- [ ] Turnstile (if used) has room for the customer's hostname.

Per customer:

- [ ] The customer has a Stripe account connected, if they sell tickets.
- [ ] `custom_domain_enabled` is on for the organization.
- [ ] The owner requests the domain and creates the DNS records. It turns **active**.
- [ ] The hostname is added to the Turnstile widget's list.
- [ ] You open the domain in a private window: the organization page renders with its colors, the
      footer reads "Powered by Revel", `/` shows only that organization, and a test purchase
      returns to the custom domain.
- [ ] Sign-up from the domain sends a verification email whose link points at the **platform**,
      and confirming it sends the user back to the customer's site.
