# Rate-limiting (HTTP 429) runbook

Triage for the **`ElevatedRateLimiting`** Prometheus alert (sustained HTTP 429s),
or any report of users being unexpectedly "rate limited" / "too many requests".

## What 429 means here

Throttles live in `common/throttling.py` (subclasses of `ninja_extra`'s
`AnonRateThrottle` / `UserRateThrottle`). The key fact for triage:

| Throttle base | Keyed on | Affected by the IP issue? |
|---|---|---|
| `UserRateThrottle`, **authenticated** request | `user.pk` | No — per user |
| `AnonRateThrottle`, or any throttle on an **anonymous** request | client IP via `get_ident()` | **Yes** |
| `OAuthTokenThrottle`, `OAuthRegistrationThrottle` (`_IPRateThrottle` mixin) | client IP via `get_ident()` for **every** caller, authenticated or not | **Yes** |

Default rates (`common/throttling.py`): `AnonDefaultThrottle` 150/min, `UserDefaultThrottle`
200/min, `WriteThrottle` 100/min, `AuthThrottle` 100/min, `OTPThrottle` 100/min per user. Newer,
narrower ones: `OrgUserLookupThrottle` 30/hour per user,
`MemberRegisterBulkInviteThrottle` 5/day per user, `MemberRegisterInviteLookupThrottle` 20/min
per client (anonymous invitation-link preview), `OAuthTokenThrottle` 60/min and
`OAuthRegistrationThrottle` 10/hour per IP.

Throttling uses the **stock** `get_ident()` — none of the throttle classes in
`common/throttling.py` override it, and none call `get_client_ip()`. With `NUM_PROXIES=1`, `get_ident()`
returns the **last** `X-Forwarded-For` entry, which in production is the
**Cloudflare edge IP**, not the visitor. So anonymous users behind the same
Cloudflare egress IP **share a throttle bucket** — the most likely cause of
*false-positive* 429s for legitimate users.

!!! note "Throttling keys on the proxy IP, not `X-Real-IP`"

    Since 1.62.4, `common/client_ip.py` resolves the real visitor IP from
    `X-Real-IP` (which Caddy sets from Cloudflare's `CF-Connecting-IP`), and request
    logging, the geo middleware, and impersonation audit all read it. **Throttling
    does not** — it still keys on the proxy IP via stock `get_ident()`
    (`X-Forwarded-For` + `NUM_PROXIES`), so the shared-bucket collision above is
    unchanged until throttles are migrated onto `get_client_ip()`.

See backend issue #479 and infra#3 for the remaining fix (key throttles on the real
visitor IP too). This is **not** a bypass — the current key is unspoofable; the risk
is collateral throttling, not evasion.

The lowest, most collision-prone limits are anonymous **registration**
(`UserRegistrationThrottle`, 100/day) and the OAuth dynamic client registration
(`OAuthRegistrationThrottle`, 10/hour). The `_IPRateThrottle` mixin exists only to stop a session
cookie from switching the throttle off; it keys on the same `get_ident()` value, so it shares the
proxy-IP collision.

## Investigate

The alert fires off the Prometheus metric
`django_http_responses_total_by_status_total{status="429"}`. To see *who/what* is
being throttled, read the logs with the Loki CLI (see the `loki-logs` skill):

```bash
# All 429s in the last hour, with the offending path + client IP
.venv/bin/python scripts/loki_logs.py web --status-code 429 --since 1h

# Which endpoints? (eyeball the path= metadata lines)
.venv/bin/python scripts/loki_logs.py web --status-code 429 --since 6h -n 500

# Is it concentrated on one (Cloudflare) IP? Pull JSON and group by ip_address.
.venv/bin/python scripts/loki_logs.py web --status-code 429 --since 6h -n 500 --json
```

In Grafana: query `sum by (view) (rate(django_http_responses_total_by_view_transport_method_total{status="429"}[5m]))`
to see which views are throttling.

## Decide

- **Concentrated on registration/login from a few Cloudflare IPs, during a traffic
  spike** → almost certainly legitimate users colliding on a shared Cloudflare-edge
  bucket. Mitigation options:
  - Prioritise infra#3 (real visitor IP) so buckets are per-user.
  - Short term, if a launch is underway, consider raising the specific anonymous
    `rate` in `common/throttling.py` (e.g. `UserRegistrationThrottle`).
- **A single endpoint hammered, high volume, abusive pattern** → working as intended
  (throttle is doing its job). No action beyond confirming it's not a customer.
- **Authenticated endpoints throwing 429** → keyed on `user.pk`, so it's a genuine
  per-user limit (e.g. `ExportThrottle` 1/min); check whether the limit is too tight
  for the workflow rather than an IP problem.

## Escape hatch

`DISABLE_THROTTLING=True` (env, `settings/base.py`) turns **all** throttling off.
Use only for load testing or a confirmed throttling incident blocking real users —
it removes brute-force/DoS protection while set.
