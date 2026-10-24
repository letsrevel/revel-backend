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
only in the Django admin (never in an API edit schema) and can be changed only by **superusers**
(it is read-only for other staff). Brand colors do not need it.

1. In the Django admin open **Organizations**, pick the organization, and in the **Custom domain**
   section tick `custom_domain_enabled`. The `custom_domain` field below it is read-only: it holds
   the hostname of the active domain and is maintained by the domain service.
2. The organization's owner can now request a domain (see the [organizer
   guide](organizers.md#custom-domain)).

Enabling is a trust decision: attendees log in on a page under a hostname whose DNS the
organization controls. Read [the password trade-off](../../architecture/white-label.md#the-password-trade-off)
before enabling it for an organization you do not know.

The flag is also the **kill switch**. Turning it off for an organization, at any time, stops its
hostname resolving, stops dynamic CORS for that host, withdraws `custom_domain` from API payloads
and sends links and mail back to the platform. The stored hostname and the domain row are kept, so
turning the flag back on restores the domain without a new request (but not the sessions that were
issued on it: see [Switch back on](#switch-back-on)). The full procedure, with
timings and verification steps, is the [kill-switch runbook](#kill-switch-runbook).

## Kill-switch runbook

Use the kill switch when an organization's domain must stop serving **now**, without touching DNS,
Cloudflare or the organization's data. Typical reasons: a [domain-problem alert](#domain-problem-alerts)
you cannot resolve with the organizer, a suspected DNS takeover or a fake login page on a customer
hostname, abuse reported on a customer site, or a customer whose contract ended.

!!! note "Who can do this"
    Only **superusers**. For other staff the `custom_domain_enabled` field is read-only in the admin.

### Switch off

1. In the Django admin open **Organizations**, pick the organization, and in the **Custom domain**
   section untick `custom_domain_enabled`. Save.
2. Do nothing at Cloudflare or at the customer's DNS. The hostname, the Cloudflare custom hostname
   and the certificate all stay as they are.

### What happens, and how fast

| Surface | Effect | Takes effect |
|---|---|---|
| Tenant lookup (`/api/organizations/by-domain/{host}`) | Answers 404, so the frontend serves a 404 on that host | The admin save drops the cached entry, so normally at once. Worst case **60 s** (the backend lookup cache TTL, if an invalidation is skipped or races a reader) |
| Dynamic CORS | The host is no longer an allowed origin, so API calls from the tenant site fail | Same as the lookup: at most 60 s |
| API payloads | `custom_domain` is `null` on every organization and event payload | Immediately (read from the database on each request) |
| Links in emails and notifications | Fall back to the platform URL | At most **60 s**: the organization link info used when rendering an email is cached for 60 s |
| Tenant email sender | Mail from the organization goes out from the platform sender again | With the link info, at most 60 s |
| Tenant sessions (logins made on that host) | Access tokens answer 401 (public pages treat the visitor as anonymous) and refresh tokens are refused and blacklisted, because the host no longer resolves. Re-enabling the switch does not revive them: every session issued before it was switched back on stays refused, and users sign in again | Same as the lookup: at most 60 s |
| Organizer's custom-domain endpoints | Answer 403 | Immediately |
| Sync task | Skips the organization | From the next run |

The frontend adds one delay of its own: every frontend replica caches the by-domain answer for up
to **60 s**, so a replica can keep serving the tenant site for that long after the backend has
stopped answering. The `301` the frontend sends from an organization's platform URLs to its custom
domain is marked `Cache-Control: no-store`, so browsers do not keep following it once the domain is
gone.

Plan on about **one minute** end to end.

### Verify

Replace `<host>` with the customer hostname and `<api>` with your API origin.

1. **Lookup is gone.** `curl -s -o /dev/null -w '%{http_code}\n' https://<api>/api/organizations/by-domain/<host>`
   answers `404` (retry for up to a minute).
2. **CORS is gone.** `curl -si -X OPTIONS https://<api>/api/organizations/ -H 'Origin: https://<host>' -H 'Access-Control-Request-Method: GET'`
   carries no `Access-Control-Allow-Origin` header for that origin.
3. **Payloads are clean.** The organization's payload (for example `GET /api/organizations/<slug>`)
   has `"custom_domain": null`.
4. **The platform serves it.** Open the organization on the platform host: it renders there with no
   redirect to the custom domain.
5. **Mail falls back.** Trigger a test email that links to the organization (for example an invitation
   to yourself) and check that its links point at the platform.

### Switch back on

1. Tick `custom_domain_enabled` again and save. **No DNS or Cloudflare work is needed**: the domain
   row, the stored hostname, the Cloudflare custom hostname and the certificate were never removed.
   Switching on also signs out **every session issued on the domain before that moment** (refresh
   tokens issued earlier are refused and blacklisted on their next use), including ones that were
   never presented while the switch was off, so users sign in again. This is deliberate: the
   switch is the response to a suspected DNS takeover, and a stolen session must not come back with
   it.
2. Allow the same windows (about a minute) and repeat the verification in reverse: the by-domain
   lookup answers 200 and the platform URL redirects to the custom domain again.

!!! warning "Re-enable caveat: a stale domain can resolve for up to 15 minutes"
    While the switch is off, the [sync task](#the-sync-task) **skips** the organization, so a domain
    that Cloudflare deletes in the meantime (for example after the customer's CNAME was broken for
    its seven-day grace period) is not marked failed. After you switch back on, that domain
    resolves as if it were healthy until the next sweep, **up to 15 minutes**, and then fails. If
    the switch was off for a long time, ask the owner to press **Check now** right after you
    re-enable it, so a dead domain is marked failed at once instead of at the next sweep.

## Cloudflare for SaaS

Revel provisions customer hostnames through [Cloudflare for
SaaS](https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/) (Custom
Hostnames) on **your** zone. Customers point a CNAME at a **fallback origin** you control, Cloudflare
validates the hostname, issues and renews the certificate, and proxies traffic to your origin.

### Setup

1. **Enable Cloudflare for SaaS** on the zone that holds your platform domain (SSL/TLS, Custom
   Hostnames). On the Free plan this needs a payment method on the account (the dashboard asks for
   it). The [plans
   page](https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/plans/)
   lists 100 hostnames as included on Free, Pro and Business, with a per-hostname price beyond that.
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
5. **Set the zone to Full (strict) and keep "Always Use HTTPS" off.** For custom hostnames
   Cloudflare connects to your origin with the *customer's* hostname as both the TLS server name
   (SNI) and the `Host` header, and under Full (strict) it verifies the certificate. The origin
   therefore needs a publicly trusted certificate for every customer hostname. A Cloudflare Origin
   CA certificate cannot cover customer domains, and rewriting the SNI to the fallback hostname is
   not available on the Free plan, so the setup uses Caddy **on-demand TLS** (see
   [Caddy](#caddy)). It can only validate over HTTP-01, and that challenge comes in through
   Cloudflare, so "Always Use HTTPS" (which redirects port 80) must stay off for the zone. Revel
   does not set `custom_origin_server` or an origin SNI when it creates hostnames.

!!! warning "One fallback origin per zone"
    Cloudflare has a single fallback origin per zone, and **every** custom hostname of the zone is
    routed to it. Two Revel deployments on the same zone (for example a beta slot and production)
    cannot both serve custom domains at the same time: whichever hostname the fallback origin
    points at receives all the traffic. On the hosted platform, production uses
    `customers.letsrevel.io` and the beta slot uses `customers-beta.letsrevel.io`. Moving the fallback
    origin between them is non-disruptive and applies within seconds, so do it as an explicit go-live
    step.

When an organization requests a domain, Revel calls Cloudflare to create the custom hostname with
**HTTP** domain-control validation and a DV certificate, so the customer normally needs only the
CNAME. Revel shows the organizer the CNAME and the TXT records Cloudflare asks for (hostname
ownership, certificate validation).

Cloudflare activates a hostname on its CNAME alone, so Revel does not trust that. A PENDING domain
only becomes ACTIVE once the **ownership TXT record** Cloudflare issued is published in public DNS,
checked over DNS-over-HTTPS against `cloudflare-dns.com` (5 s timeout). If the record is missing,
wrong, or the lookup fails, the domain stays PENDING and `last_errors` names the record to add;
the next sync retries. The check fails closed, so **the web and Celery containers must be allowed
outbound HTTPS to `cloudflare-dns.com`** (the web container runs the same sync for the organizer's
**Check now**); otherwise no domain ever activates. The gate applies to the sync only: an admin
who activates a domain by hand in the Django admin skips it, so verify ownership yourself first.

Provisioning is on only when **both** `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` are set.

## Environment variables

### Backend

| Variable | Default | Meaning |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | empty | API token with *SSL and Certificates: Edit* on the zone, plus *Account → Turnstile Sites → Edit* when the Turnstile automation is on. |
| `CLOUDFLARE_ZONE_ID` | empty | Zone that owns the custom hostnames. Provisioning is enabled only when this **and** the token are set. Neither alone does anything. |
| `CLOUDFLARE_ACCOUNT_ID` | empty | Cloudflare account that owns the Turnstile widget. With `CLOUDFLARE_API_TOKEN` and `TURNSTILE_SITE_KEY` it turns on the [Turnstile hostname automation](#turnstile). |
| `TURNSTILE_WIDGET_HOSTNAME_LIMIT` | `10` | Hostnames the widget can hold (Free plan: 10, Enterprise: up to 200). The sync alerts staff when more active domains exist than fit. `0` turns the check off. |
| `CUSTOM_DOMAIN_LIMIT` | `9` | Hard cap on live (pending plus active) custom domains platform-wide. A new request is refused with 409 at the limit. `0` means unlimited. See [the domain cap](#the-domain-cap). |
| `CUSTOM_DOMAIN_CNAME_TARGET` | empty | The fallback origin hostname organizers CNAME to, for example `customers.example.com` (the hostname registered as the zone's fallback origin). A bare hostname: no scheme, path, port or spaces. It is lowercased and a trailing dot is removed. Shown to organizers as the CNAME value. |
| `CUSTOM_DOMAIN_URL_TEMPLATE` | `https://{hostname}` | Origin of an organization's active domain, used for links in emails and notifications. Must contain `{hostname}` and start with `http://` or `https://`. Local development and E2E use `http://{hostname}:5173`. |

Related variables that matter for white-label: `BASE_URL` and `FRONTEND_BASE_URL` (a custom
hostname can never equal, or sit under, the hosts and registrable apex of these two),
`ORG_EMAIL_DOMAIN` (see [email rollout](#email-rollout-order)) and `TURNSTILE_SITE_KEY` /
`TURNSTILE_SECRET_KEY` (see [Turnstile](#turnstile)). The API's own `ALLOWED_HOSTS` and
`CORS_ALLOWED_ORIGINS` do **not** need to list customer domains: the browser talks to the API host,
and CORS for active custom domains is allowed dynamically. `CORS_ALLOWED_ORIGINS` must still contain
the platform frontend origin, because protected media now takes its CORS answer from Django (see
[Caddy](#caddy)).

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
CLOUDFLARE_ACCOUNT_ID=...          # only with Turnstile: see the Turnstile section
CUSTOM_DOMAIN_LIMIT=9
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
and proxies to the frontend. The hosted platform's Caddyfiles do this
([letsrevel/infra#68](https://github.com/letsrevel/infra/pull/68)); if you run your own, its catch-all
block must meet these requirements:

- **On-demand TLS, gated by an ask check.** Caddy obtains a Let's Encrypt certificate for a customer
  hostname the first time it is requested, over HTTP-01 only (the challenge arrives through
  Cloudflare, so "Always Use HTTPS" must stay off). Before it does, it asks the backend through
  `on_demand_tls { ask http://<backend>:8000/api/tls/ask }` (Caddy appends `?domain=<host>`). The
  endpoint answers 200 only for an ACTIVE domain whose organization has the kill switch on, and 404
  for anything else, so nobody can make Caddy burn certificate quota on arbitrary names. Point the
  `ask` URL straight at the backend container over the internal network; the endpoint is
  unthrottled by design (all asks come from the proxy's IP, and a throttle would let junk handshakes
  block real tenants), cached like the by-domain lookup, and hidden from the API docs. It needs no
  loopback shim and no hostname filter in Caddy. Caddy sends the container name as the `Host`, so
  `ALLOWED_HOSTS` must include it (the `web` container: `web`). Deploy the backend before pointing
  Caddy at it: an older backend answers 404 and every certificate is refused.
- **Overwrite the forwarding headers on every frontend vhost, the catch-all included.** Proxy to the
  frontend with the same headers as the main frontend block, and set `X-Forwarded-Host {host}` and
  `X-Forwarded-Proto https`, replacing any value a client sends. The frontend trusts these to decide
  which organization it is serving.
- Send HSTS **without** `includeSubDomains` for foreign domains. They are not yours.
- Never buffer SSR streaming responses, as for the main frontend block.
- **Protected media** used to be served with a CORS header pinned to `FRONTEND_DOMAIN`. It now echoes
  the allowed origin from Django's answer to the `forward_auth` subrequest (and strips any value
  the client sent), so it works for the platform and for active custom domains alike. This only
  works while `CORS_ALLOWED_ORIGINS` contains the platform frontend origin.
- Caddy reads `.env` when its container starts, so a new `CUSTOM_DOMAIN_CNAME_TARGET` needs
  `docker compose up -d caddy`; `caddy reload` does not pick it up.

## Turnstile

The Turnstile widget only renders on hostnames in its **hostname allowlist**, and a listed hostname
also covers its subdomains ([API reference](https://developers.cloudflare.com/api/resources/turnstile/subresources/widgets/):
"the widget will only work on these domains, and their subdomains"). Without the custom domain on
that list, sign-up there fails the captcha. Revel keeps the list in step itself.

**The automation.** Set `CLOUDFLARE_ACCOUNT_ID` next to `CLOUDFLARE_API_TOKEN` and the existing
`TURNSTILE_SITE_KEY` (the widget it manages). It is off unless all three are set, and independent of
`CLOUDFLARE_ZONE_ID`. The API token needs one more permission besides *Zone → SSL and Certificates →
Edit*: **Account → Turnstile Sites → Edit** (API name "Turnstile Sites Write"), scoped to your
account. Without it the sync logs a Cloudflare error and emails staff.

It is a reconciliation, not a list of adds and removes. Revel reads the widget
(`GET /accounts/{account_id}/challenges/widgets/{sitekey}`) and computes the list it should have:

- every current entry that is **not** a hostname Revel manages, so `letsrevel.io` and anything you
  added by hand stays, plus
- the hostnames of all **active** custom domains of organizations whose `custom_domain_enabled`
  kill switch is on.

A hostname is "Revel-managed" when it is the hostname of any domain row, in any status. Only if the
list differs does Revel write it back (`PUT` of the whole widget configuration with only `domains`
changed, so name, mode and every other setting are preserved). It runs once per
[sync sweep](#the-sync-task), and right after a domain becomes active or stops being active (failed,
removed, kill switch flipped, organization deleted), as a Celery task dispatched on commit
(`events.reconcile_turnstile_hostnames`). A failure never blocks activation: it is logged
(`turnstile_hostnames_sync_failed`) and staff get one email per outage (the sweep retries every 15
minutes, and a later failure after a good run alerts again).

**The limit.** One widget holds 10 hostnames on Free and at most 200 on Enterprise (see the
[Turnstile plans](https://developers.cloudflare.com/turnstile/plans/) page), and the platform host
takes one. Set `TURNSTILE_WIDGET_HOSTNAME_LIMIT` to your plan's number. If more active domains exist
than fit, Revel writes the ones that fit (longest-serving first), logs
`turnstile_hostnames_over_capacity` and emails staff; sign-up on the rest fails until you make room.
Revel reads a single widget (one site key and secret pair), so more capacity means a plan with a
higher limit. The [domain cap](#the-domain-cap) is what normally keeps you under it.

Self-hosters who leave the Turnstile keys empty have no Turnstile and nothing to do. If you have
Turnstile but do not set `CLOUDFLARE_ACCOUNT_ID`, add each custom domain to the widget by hand when
you activate it, and remove it when the domain goes.

### The domain cap

`CUSTOM_DOMAIN_LIMIT` (default **9**) is a hard cap on live custom domains platform-wide. It is 9
because the Turnstile Free plan allows 10 hostnames per widget and the platform host takes one. The
real constraint is "domains on the widget", which is the **active** ones; the cap counts **pending
plus active** because every pending domain is a future widget entry, and it counts them whatever the
organization's kill switch says (a switched-off domain returns to the widget when the switch goes
back on). Failed and removed domains do not count.

`request_domain` refuses a new domain at the limit with a 409 ("The platform has reached its limit of
custom domains. Please contact support."). The check runs before anything is created at Cloudflare and
again under a database advisory lock at insert time, so concurrent requests cannot overshoot.
Raise the limit after upgrading the plan (and `TURNSTILE_WIDGET_HOSTNAME_LIMIT` with it), or set it to
`0` for unlimited, for example when you run without Turnstile.

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
| Hostname **and** certificate `active` | becomes **active**, but only once the ownership TXT record is published in public DNS (otherwise it stays pending) | stays active (not re-checked) |
| Hostname `blocked` or `deleted`, or certificate `deleted` | becomes **failed** | becomes **failed** and is withdrawn from the organization |
| Hostname `moved` (validation exhausted, or the CNAME no longer points to the fallback origin) | becomes **failed** | becomes **failed** and is withdrawn from the organization; every tenant session issued on it is signed out |
| A certificate `*_timed_out` status | becomes **failed** | stays **active**; the problem is recorded |
| Anything else (for example `active_redeploying`) | unchanged | unchanged |

`moved` fails an **active** domain immediately because the DNS no longer points at Revel: the domain
is not serving our site anyway, and the hostname may have been repointed at a server the organizer
controls. Failing it withdraws the hostname, which **signs out every tenant session** issued on the
domain (access tokens are refused and refresh tokens are blacklisted on their next use; see
[the password trade-off](../../architecture/white-label.md#the-password-trade-off)). It is
recoverable: once the CNAME is fixed the organizer requests the domain again. A certificate timeout
only records the problem on an active domain, since DNS still reaches us. DNS records and error
messages are refreshed on every run either way.

**DNS hints.** A domain that was **pending** and is not becoming active also gets DNS pre-flight
hints appended to `last_errors` (CNAME missing or wrong, records at the doubled name, CAA blocking
Let's Encrypt, a root-domain hostname, plus the customer's DNS host and a help link). They are the
first thing to read when a customer says "it stays pending", and they are what the organizer sees on
**Check now**, so many DNS tickets answer themselves. They are advisory: the lookups go through
`cloudflare-dns.com` over DNS-over-HTTPS (about 6 to 9 queries per pending domain per run, one when the
resolver is down), a failed lookup skips its check, and nothing here can block or delay activation.
When Cloudflare already reports the hostname ready and only the ownership TXT is missing, only the
doubled-name mistake is checked, since the TXT message already says what to add. Active domains are
never diagnosed: a new `last_errors` entry on an active domain would alert staff. The DNS-host table
(`DNS_PROVIDERS` in `events/service/dns_diagnostics.py`) is plain data; add a row to recognise another
provider.

The decision is a compare-and-set: before applying a transition the task re-reads the status under
a row lock, and does nothing if the organizer removed the domain in the meantime. One domain
raising an error is logged (`custom_domain_sync_failed`, with the domain id) and does not stop the
rest. The task returns `{"synced": n, "failed": n}` and syncs no domain when Cloudflare is not
configured. The organizer's **Check now** runs the same sync for their own domain on demand.
After the domains, the sweep releases hostnames that are never proven or fixed, so an unverified request
cannot squat a hostname indefinitely. Both steps run whether or not Cloudflare is configured, skip
organizations whose kill switch is off (like the sync above) and isolate errors per domain:

- **Expire pending.** A **pending** domain created more than **7 days** ago is marked **failed**
  with the `last_errors` entry "Ownership of the hostname was not proven within 7 days." The owner
  gets the usual failed email. This also applies in manual mode: staff have 7 days to activate.
- **Retire failed.** A **failed** domain whose last change (`updated_at`) is more than **7 days** old
  is retired to **removed**: its Cloudflare hostname is deleted first when Cloudflare is configured
  (a 404 counts as gone), then the row is removed, which frees the hostname. If Cloudflare refuses
  the deletion the row stays failed and the next sweep retries (`custom_domain_release_failed`).

The windows are the constants `PENDING_EXPIRY` and `FAILED_RETENTION` in
`events/service/custom_domain_service.py` (7 days each, like the window after which Cloudflare deletes
a `moved` hostname). A domain that just expired is retired no sooner than 7 days later.

The sweep also re-asserts the denormalized `Organization.custom_domain` (see
[architecture](../../architecture/white-label.md)): a full save of a stale organization can write an old value back,
which would make an active domain stop resolving. Each repair logs a `custom_domain_denormalized_hostname_repaired`
WARNING with the organization id and clears the tenant cache; the sweep does this with or without Cloudflare.
If you see it repeatedly, something keeps overwriting the column.

Finally the sweep reconciles the [Turnstile widget](#turnstile) once (when that
automation is configured); a failure there is logged and alerted, never raised.

### Domain-problem alerts

The sync task also tells you when a **live** domain gets into trouble, so you hear about it before a
customer does. Two events exist:

| Event | When | What fires |
|---|---|---|
| `custom_domain_active_problem` | An **ACTIVE** domain that had no recorded problem now has one (a certificate `*_timed_out` status). The domain stays active | A `WARNING` log line with the domain id, hostname, organization id and the problems, and one email to platform staff |
| `custom_domain_active_failed` | An **ACTIVE** domain goes to **failed** (hostname `blocked`, `deleted` or `moved`, certificate `deleted`) and is withdrawn from the organization | A `WARNING` log line and one email to platform staff |

Alerts fire **once, on the transition**, not on every 15-minute sweep: a problem that persists does
not alert again, and a domain that recovers and later breaks again alerts again. The email is sent
by the Celery task `events.notify_custom_domain_problem` to **all active superusers and staff** (one
BCC'd message) with the hostname, the organization, the status and Cloudflare's messages. If there
are no active staff, the task logs `custom_domain_alert_no_recipients` instead. To find them in the
logs, search for the event names above.

**Recommended response.**

1. Open the domain under **Custom Domains** in the admin and read `last_errors`.
2. For `moved` or a certificate problem, ask the organizer to check their DNS: the CNAME must still
   point at your fallback origin. A `moved` domain is already failed (and its sessions signed out);
   Cloudflare keeps the hostname for several days before deleting it.
3. If you cannot resolve it quickly, or the change looks hostile (a hostname repointed at a server
   you do not recognise), use the [kill switch](#kill-switch-runbook).
4. For `custom_domain_active_failed`, the domain is already withdrawn, its tenant sessions are signed
   out and the platform URL serves the organization. The organizer can request the domain again from the admin area.

### Owner notifications

The organization **owner** is also emailed (and notified in the app and on Telegram, per their
preferences) when a domain changes state. This is separate from the staff alerts above, which cover
only ACTIVE domains and go to staff.

| Notification type | Transition | Content |
|---|---|---|
| `org_custom_domain_active` | PENDING to ACTIVE | "*hostname* is now live for *org*", the link to the domain (built for the tenant), a short checklist, and a link to the custom-domain settings page |
| `org_custom_domain_failed` | PENDING to FAILED, or ACTIVE to FAILED | The hostname, whether it was live, the current `last_errors` (DNS pre-flight hints included) and a link to the custom-domain settings page |

- **Once per transition, whoever causes it.** The hook sits in `custom_domain_service._save_status`,
  so the 15-minute sync, **Check now** and the Django admin actions (**Activate** / **Mark as failed**)
  all notify. An admin action does too: on an instance without Cloudflare the admin action *is* the
  activation, and the owner would otherwise never learn about it. Nothing is sent for **Remove**
  (the owner did it), a no-op sync, a certificate timeout on an ACTIVE domain (that stays a staff
  alert), or staff re-activating a FAILED domain.
- **Recipient: the owner only.** The custom-domain endpoints are owner-only, so staff have nothing
  to act on.
- **Not mandatory.** Both types are opt-out-able like other owner notifications (a per-type switch,
  the email switch and *silence all* apply). They are *transactional*, so a digest setting does not
  delay them.
- **Sender and links.** Revel's own mail: it goes out from the platform sender (not "Org via Revel")
  with the platform unsubscribe link and the organization's logo and name in the body. The live link
  uses the tenant origin; the settings link always uses the platform
  (`<frontend>/org/<slug>/admin/custom-domain`).
- **Languages.** English, German, Spanish, French, Italian and Portuguese. Cloudflare's own messages
  and the DNS hints inside `last_errors` are stored in English and shown as stored.

### Organization deletion

Deleting an organization cascades its domain rows, so its Cloudflare custom hostnames would
otherwise be left behind, keeping the hostname claimed and the certificate alive. When the deletion
commits, Revel dispatches the Celery task `events.delete_organization_cloudflare_hostnames`, which
deletes each hostname at Cloudflare **best effort**: a 404 counts as already gone, other failures
are logged as `custom_domain_orphaned_cloudflare_hostname`, and with Cloudflare not configured it
logs a warning and skips. The deletion itself never waits on or fails because of Cloudflare. If you
see that error in the logs, remove the leftover hostname in the Cloudflare dashboard by hand.

## Manual activation in the Django admin

Staff can manage domains under **Custom Domains** in the admin sidebar (the
`OrganizationDomain` model). Select rows and run an action:

- **Activate selected domains**: marks the domain active and publishes its hostname on the
  organization. It does **not** run the ownership TXT check, so confirm the hostname really belongs
  to the organization first. Fails for an organization with `custom_domain_enabled` off, for a removed domain,
  and when the organization already has another live domain or the hostname is taken.
- **Mark selected domains as failed**: withdraws the hostname from the organization.
- **Remove selected domains**: deletes the Cloudflare hostname (when provisioned) and frees the
  hostname for other organizations.

The status field is read-only and deleting rows is disabled, because status changes must go through
these actions: they keep the organization's `custom_domain` column and the lookup cache in step.
Use the actions rather than editing the database. Adding rows here is disabled too: a domain is
always created by the organization owner's request, which enforces the kill switch and provisions
Cloudflare when it is configured. Without Cloudflare, you then activate that pending row with the
action above (see [Running without Cloudflare](#running-without-cloudflare)).

## Running without Cloudflare

Cloudflare is optional. Leave `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` empty and Revel
runs in **manual mode**:

- A domain request only validates the hostname and records a **pending** row with the CNAME
  instruction. Nothing is provisioned, the sync task does not poll Cloudflare and the organizer's
  **Check now** has nothing to check. The sweep still expires a pending domain after 7 days and
  retires a failed one after 7 more (see [the sync task](#the-sync-task)).
- A platform admin **activates** the domain in the Django admin once DNS and TLS are ready.
- **TLS is yours.** You need a certificate for each hostname on your own proxy. The simplest route
  is to add the hostname as a site block in your Caddyfile after the organizer creates the
  CNAME (Caddy then obtains a Let's Encrypt certificate). Caddy's on-demand TLS also works, exactly
  as in the [hosted setup](#caddy): point `on_demand_tls { ask ... }` at
  `http://<backend>:8000/api/tls/ask`, which answers 200 only for the hostnames you have activated.
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

On the hosted platform, mail stays "Org via Revel" on `mail.letsrevel.io` (`ORG_EMAIL_DOMAIN`,
[letsrevel/infra#57](https://github.com/letsrevel/infra/issues/57)): a custom domain changes the
links in the mail, not the sender domain.

Account emails (verification, password reset, deletion) and unsubscribe links stay on the platform
sender and the platform URL, never on a customer domain. See [Email Sending &
Opt-Out](../../architecture/email-sending.md#sender-identity).

## Release order

Deploy the **backend before the frontend**. The frontend's tenant mode calls
`GET /api/organizations/by-domain/{host}` and reads the custom-domain fields, which only exist in the
backend release. Tenant mode stays off until `PLATFORM_HOSTS` is set on the frontend, so shipping the
frontend release changes nothing by itself. Rolling-deploy safety on the backend side is described in
the [architecture page](../../architecture/white-label.md#rolling-deploys).

## Go-live checklist

Once per instance:

- [ ] Backend `BASE_URL` and `FRONTEND_BASE_URL` are public origins, and `manage.py check` is clean
      (no `events.W001`, `events.W002`, `common.E002`, `common.W005`).
- [ ] Cloudflare for SaaS is enabled on the zone (payment method added on Free), the fallback origin
      hostname exists, is proxied and serves HTTPS, and is registered as the fallback origin. No other
      deployment on the zone needs custom domains at the same time (one fallback origin per zone).
- [ ] `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ZONE_ID` and `CUSTOM_DOMAIN_CNAME_TARGET` are set on web
      and Celery, and the services are restarted.
- [ ] The zone SSL mode is Full (strict) and "Always Use HTTPS" is off.
- [ ] Web and Celery can reach `https://api.cloudflare.com` and `https://cloudflare-dns.com`.
- [ ] The Caddy catch-all block uses on-demand TLS with the ask check, overwrites
      `X-Forwarded-Host` and `X-Forwarded-Proto`, sends no `includeSubDomains` HSTS, does not buffer
      SSR streams, and protected media echoes Django's CORS answer. `CORS_ALLOWED_ORIGINS` contains
      the platform frontend origin.
- [ ] The frontend runs with `HOST_HEADER`, `PROTOCOL_HEADER`, `PLATFORM_HOSTS`, `PLATFORM_ORIGIN`
      and `PUBLIC_CUSTOM_DOMAIN_URL_TEMPLATE`, and **without** `ORIGIN`.
- [ ] The backend `CUSTOM_DOMAIN_URL_TEMPLATE` and the frontend `PUBLIC_CUSTOM_DOMAIN_URL_TEMPLATE`
      are identical.
- [ ] The `events.sync_custom_domains` periodic task is enabled in the admin (it is by default).
- [ ] `ORG_EMAIL_DOMAIN` is set and authenticated (DKIM, DMARC), or you have accepted the apex
      reputation trade-off described above.
- [ ] Turnstile (if used): `CLOUDFLARE_ACCOUNT_ID` is set, the API token also has *Account → Turnstile
      Sites → Edit*, and `TURNSTILE_WIDGET_HOSTNAME_LIMIT` and `CUSTOM_DOMAIN_LIMIT` match your plan.
- [ ] You have read the [kill-switch runbook](#kill-switch-runbook) and know who on the team is a
      superuser, and the [domain-problem alerts](#domain-problem-alerts) reach a mailbox someone
      reads (they go to every active superuser and staff user).

For each new organization, follow [Onboarding a new white-label
organization](#onboarding-a-new-white-label-organization).

## Onboarding a new white-label organization

Do these in order for every organization. Steps 1 and 6 are yours; the organization owner does
steps 2 and 3; Revel does steps 4 and 5.

1. **Enable the flag.** In the Django admin, as a superuser, tick `custom_domain_enabled` on the
   organization (see [Enabling custom domains](#enabling-custom-domains-for-an-organization)). Before
   that, confirm you trust the organization with a login page under its own name, and that it has a
   Stripe account connected if it sells tickets.
2. **The owner requests the domain.** In the organization settings, **Custom domain**, the owner
   enters a subdomain such as `events.example.org`. The domain is **Pending** and the page lists the
   DNS records to create.
3. **The owner creates the DNS records.** The **CNAME** from the hostname to the fallback origin
   (`customers.letsrevel.io` on the hosted platform), and the **ownership TXT** record the page
   shows. Both are required: the CNAME alone is not enough.
4. **Activation.** Revel's sync (every 15 minutes, or the owner's **Check now**) activates the
   domain once Cloudflare reports the hostname and certificate `active` **and** the ownership TXT
   record is visible in public DNS. If it stays pending, read `last_errors` under **Custom Domains**
   in the admin; the ownership message names the missing record, and the DNS hints (wrong or missing
   CNAME, doubled name, CAA, root domain, the customer's DNS host) say what else to fix. Caddy issues the origin
   certificate on the first request, because the ask check now answers 200.
5. **The Turnstile widget updates itself.** Right after activation Revel adds the hostname to the
   registration widget's list (see [Turnstile](#turnstile)). If it does not within a minute, check
   the staff mailbox and the logs for `turnstile_hostnames_sync_failed` or
   `turnstile_hostnames_over_capacity`. Without `CLOUDFLARE_ACCOUNT_ID`, add the hostname by hand.
6. **Check the base features.** Open the domain in a private window: the organization page renders
   with its colors, the footer reads "Powered by Revel", `/` shows only that organization's events,
   and a test purchase returns to the custom domain.
7. **Check sign-up and email.** Sign up from the domain: the verification email links to the
   **platform**, and confirming it sends the user back to the customer's site. Then log in on the
   domain; the session is bound to that host and ends if the domain is later removed, failed or
   switched off.

To take the organization offline later, use the [kill-switch runbook](#kill-switch-runbook). The
hostname leaves the Turnstile widget on its own when the domain is removed, fails or is switched off.
