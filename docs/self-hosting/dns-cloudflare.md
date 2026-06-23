# DNS & Cloudflare

Revel uses Caddy to obtain and renew TLS certificates automatically via Let's Encrypt. For that to
work, your DNS records must resolve to the server and ports 80/443 must be reachable. This page
covers the records you need and the one Cloudflare gotcha that trips most people up.

## DNS records

Create A records (and AAAA records if your host has IPv6) pointing the relevant hostnames at your
server's public IP.

For a **Slim** instance you need two:

| Type | Name | Value |
| --- | --- | --- |
| A | `<FRONTEND_DOMAIN>` | your server IPv4 |
| A | `<API_DOMAIN>` | your server IPv4 |

For a **Full** instance, also add the management subdomains:

| Type | Name | Value |
| --- | --- | --- |
| A | `grafana.<your-domain>` | your server IPv4 |
| A | `docs.<your-domain>` | your server IPv4 |

Add the matching AAAA records if you serve over IPv6.

## The Cloudflare orange-cloud caveat

If your domain is on Cloudflare, the proxy ("orange cloud") interferes with Caddy's first
certificate issuance over the HTTP-01/TLS-ALPN challenge.

!!! warning "Disable the proxy during first cert issuance"
    Set each record to **"DNS only" (grey cloud)** before you first bring the stack up. Let Caddy
    obtain its certificates, confirm the site loads over HTTPS, and *then* re-enable the proxy
    (orange cloud). If you start with the proxy on, certificate issuance will fail and the site
    will not come up — and the failures can be slow and confusing to diagnose.

After certificates are issued, re-enabling the proxy is safe and gives you Cloudflare's CDN, DDoS
protection, and edge caching.

## The Cloudflare Caddyfile variant

When you proxy through Cloudflare, the client IP that reaches Caddy is Cloudflare's, not the real
visitor's — the real IP arrives in the `Cf-Connecting-Ip` header. The setup wizard selects a
**Cloudflare Caddyfile variant** that configures `trusted_proxies` for Cloudflare's IP ranges and
reads `Cf-Connecting-Ip`, so rate limiting, logging, and geolocation see the correct client
address.

!!! note
    If you change your proxy setup later (turning Cloudflare on or off), make sure the active
    Caddyfile variant matches. Running the plain variant behind the Cloudflare proxy means every
    request appears to come from a Cloudflare IP; running the Cloudflare variant *without* the
    proxy would trust a header an attacker could forge.
