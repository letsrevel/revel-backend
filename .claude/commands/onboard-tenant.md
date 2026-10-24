---
description: Onboard, diagnose or offboard a white-label custom-domain tenant (pre-flight, enable, Turnstile, activation, warm-up, smoke test)
argument-hint: "<onboard|status|offboard> <org-slug> <hostname> [--env prod|beta]"
---

<!-- Operator runbook for white-label tenants. Holds no secrets. -->

Walk the operator through one white-label tenant. Arguments: **$ARGUMENTS**

## Rules (read first)

- **No `ssh revel` (prod) or `ssh personal` (beta/demo) until the user has explicitly approved it in this
  conversation.** Every server command below is optional; prefer public endpoints and the Cloudflare API.
- **Cloudflare writes need explicit approval, one at a time** (Turnstile hostname list, custom hostnames).
  Reads are fine. Never touch the zone SSL mode, Always Use HTTPS, the fallback origin or DNS records of
  `letsrevel.io` in this command. Never touch the **customer's** DNS: tell them what to create.
- **No PII in context**: print org slugs, ids, hostnames and flags. Never print owner emails or attendee data.
- Secrets: read tokens with `read -rs` into env vars. Never echo them or put them in files.
- `custom_domain_enabled` is a **superuser trust decision**. The user ticks it; you never flip it
  through a shell unless the user explicitly asks and approves the ssh.
- Outbound mail to the customer: draft only (a Gmail draft is fine). Never send without explicit approval.

Environment table (`--env`, default `prod`):

| | prod | beta |
|---|---|---|
| API | `https://api.letsrevel.io` | `https://beta-api.letsrevel.io` |
| Frontend | `https://letsrevel.io` | `https://beta.letsrevel.io` |
| CNAME target | `customers.letsrevel.io` | `customers-beta.letsrevel.io` |
| ssh / compose dir | `revel` / `~/infra` | `personal` / `~/infra/revel-beta` |
| Zone id | `$CF_ZONE_ID` (from your environment) | same |
| Turnstile widget | "Revel signup" | same (check whether beta has Turnstile keys) |

Set `API`, `FE`, `TARGET`, `ZONE` (= `$CF_ZONE_ID`), `HOST` (the customer hostname, lowercased, no scheme) and `SLUG`.

## onboard

### 1. Pre-flight (read-only, from the laptop)

Run and present one table (check / result / verdict):

```bash
doh() { curl -s -H 'accept: application/dns-json' "https://cloudflare-dns.com/dns-query?name=$1&type=$2" | jq -c '[.Status, (.Answer // [] | map(.data))]'; }
doh "$HOST" CNAME          # expect $TARGET. (or empty if not created yet)
doh "$HOST" SOA            # an SOA AT this exact name => zone apex: STOP, ask for a subdomain
doh "_cf-custom-hostname.$HOST" TXT
# CAA: walk up from HOST until a level answers; it must allow letsencrypt.org AND pki.goog (or have no CAA)
h="$HOST"; while [ "${h#*.}" != "$h" ]; do r=$(doh "$h" CAA); echo "$h $r"; echo "$r" | grep -q 'issue' && break; h="${h#*.}"; done
doh "${HOST#*.}" NS        # cloudflare.com nameservers => tell them: CNAME "DNS only" (grey) by default
```

Also:
- Hostname shape: not under `letsrevel.io`, no wildcard/port/IP, not punycode (`xn--`) unless the user
  confirms it is legitimate.
- Cloudflare SaaS quota (read):
  `curl -s -H "Authorization: Bearer $CF_TOKEN" "https://api.cloudflare.com/client/v4/zones/$ZONE/custom_hostnames/quota" | jq .result`
- Turnstile capacity (read; needs a token with Turnstile read access and `$CF_ACCOUNT`):
  `curl -s -H "Authorization: Bearer $CF_TS_TOKEN" "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/challenges/widgets" | jq '.result[] | {name, sitekey, n: (.domains|length), domains}'`
  Free plan: **10 hostnames per widget**. If "Revel signup" is full, STOP and tell the user: sign-up on
  the new domain will fail. Options: remove a stale hostname, an extra widget (needs Revel code), or Enterprise.
- Existing hostname row: `curl -s -o /dev/null -w '%{http_code}\n' "$API/api/organizations/by-domain/$HOST"`
  (200 means some org already serves it: STOP). The TLS ask endpoint must say the same, and it is
  the one Caddy consults: `curl -s -o /dev/null -w '%{http_code}\n' "$API/api/tls/ask?domain=$HOST"`
  (404 now, 200 once the domain is ACTIVE).

### 2. Trust gate (ask the user)

Confirm: who the customer is and the contract; Stripe connected if they sell; contact email verified
(Reply-To only uses a verified one); the user accepts the password trade-off
(`docs/architecture/white-label.md#the-password-trade-off`). Wait for an explicit yes.

### 3. Enable (the user does it)

Ask the user (a superuser) to open the Django admin → Organizations → `$SLUG` → Custom domain → tick
`custom_domain_enabled` → Save. Verify nothing else: the flag is not public until a domain is active.

### 4. Instructions for the customer (draft)

Ask the owner to request `$HOST` under org settings → Custom domain, then have their DNS admin create
**exactly** what the page shows:

| Type | Name | Value | Proxy |
|---|---|---|---|
| CNAME | `$HOST` | `$TARGET` | DNS only (grey) if on Cloudflare |
| TXT | `_cf-custom-hostname.$HOST` | the UUID shown on the page (copy-paste) | n/a |

Warn about providers that append the zone to the name field (enter `events`, not `events.example.org`),
CAA (allow `letsencrypt.org` and `pki.goog`), and that it can take up to ~15 min after DNS is visible.
Offer to save this as a Gmail draft. Do not send.

### 5. Watch activation

After the owner has requested it:

```bash
curl -s -H "Authorization: Bearer $CF_TOKEN" "https://api.cloudflare.com/client/v4/zones/$ZONE/custom_hostnames?hostname=$HOST" \
  | jq '.result[0] | {status, ssl: .ssl.status, verification_errors, ssl_errors: .ssl.validation_errors, txt: .ownership_verification}'
doh "_cf-custom-hostname.$HOST" TXT      # must equal .ownership_verification.value
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/organizations/by-domain/$HOST"   # 200 = Revel ACTIVE
curl -s -o /dev/null -w '%{http_code}\n' "$API/api/tls/ask?domain=$HOST"            # 200 = Caddy will issue a certificate
```

Map what you see to a fix:

| Symptom | Fix |
|---|---|
| `custom hostname does not CNAME to this zone.` | CNAME missing, wrong name (double suffix), proxied in the same CF account, or at an apex |
| CF `active`/`active` but by-domain 404 | TXT missing or wrong value, or negative-cached: wait, then ask the owner to press **Check now** |
| `CAA records block issuance` | Customer adds CAA for the CA named in the error (and `letsencrypt.org`) |
| `*_timed_out` / FAILED | Fix DNS, then the owner requests again (a FAILED row is retired automatically) |
| 409 "already in use" | Another org's row holds it (#1168): find it in admin → Custom Domains, confirm with the user, then "Remove selected domains" |

Optional (after ssh approval, only once b8 exists):
`ssh revel "cd infra && docker compose exec -T web python manage.py custom_domain_status $HOST"`.

### 6. Turnstile (Cloudflare write: ask first)

Once by-domain and `/api/tls/ask` answer 200: show the current `domains` list of "Revel signup", the list with `$HOST`
added, and the count vs the cap. On approval, PUT the full widget body back (read-modify-write; keep
`name`, `mode` and every existing domain):

```bash
W=$(curl -s -H "Authorization: Bearer $CF_TS_TOKEN" "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/challenges/widgets/$SITEKEY" | jq '.result')
echo "$W" | jq --arg h "$HOST" '{name, mode, domains: ((.domains + [$h]) | unique)}' > "$TMPDIR/widget.json"
curl -s -X PUT -H "Authorization: Bearer $CF_TS_TOKEN" -H 'Content-Type: application/json' \
  "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT/challenges/widgets/$SITEKEY" --data @"$TMPDIR/widget.json" | jq '{success, domains: .result.domains}'
rm -f "$TMPDIR/widget.json"
```

(Check the endpoint and body against the Cloudflare API docs MCP the first time you run this.)

### 7. Warm the origin certificate

```bash
for i in $(seq 1 12); do c=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST/"); echo "$i $c"; [ "$c" = 200 ] && break; sleep 5; done
curl -sI "https://$HOST/" | grep -iE '^HTTP|strict-transport|x-robots'
```

A persistent 525/526: the origin cert failed (`/api/tls/ask` not answering 200 for the host, Caddy's
`ask` URL misconfigured, HTTP-01 blocked, CAA forbids Let's Encrypt). Diagnosis needs Caddy logs; ask for ssh approval, then
`docker logs caddy --since 15m 2>&1 | grep -E "$HOST|on_demand|permission|acme" | tail -20`.

### 8. Smoke test (private window), then report

- `https://$HOST/` shows the org with its colors and "Powered by Revel"; only its events are listed.
- `/create-org` and `/org/$SLUG/admin` redirect to the platform; the platform org URL 301s to `$HOST`.
- Sign up a test user on `$HOST` (Turnstile renders); the verification mail links to the **platform**.
- A test purchase returns to `$HOST`.
- Protected-media CORS echoes the tenant origin:
  `curl -sI -H "Origin: https://$HOST" "$API/media/protected/x?exp=1&sig=x" | grep -i access-control-allow-origin`

Report: a checklist of what passed, what is pending, and the Turnstile slots left.

## status

Run the step 1 checks, then steps 5, 7 and the Turnstile membership read, and give the most likely
cause with its fix (use the table in step 5). Don't change anything.

## offboard

1. Ask whether the goal is a **pause** (the kill switch: the user unticks `custom_domain_enabled`;
   about 1 min server-side, 5 min in browsers) or **removal**.
2. Removal: the owner presses **Remove**, or the user runs admin → Custom Domains → "Remove selected
   domains". Verify: by-domain 404, and the Cloudflare `custom_hostnames?hostname=$HOST` result is empty.
3. Turnstile: after approval, remove `$HOST` from the widget (same read-modify-write as step 6, `- [$h]`).
4. Tell the customer they can delete the CNAME and TXT records.
5. If the org was deleted instead, check that the Cloudflare hostname is gone. If it isn't, it was
   orphaned (`custom_domain_orphaned_cloudflare_hostname` in logs, see `/logs`); delete it after
   approval.
