# Troubleshooting

The known failure modes of a self-hosted instance, each with its cause and fix.

## `web` container crash-loops on startup (cities migration)

**Cause:** the `0002_load_cities` migration used to hard-fail with `FileNotFoundError` when the
full `worldcities.csv` was absent — and that CSV is gitignored and not shipped in the published
image, so a fresh instance would crash-loop forever.

**Fix:** this is now handled automatically. The migration falls back to the bundled
`worldcities.mini.csv` (50 cities) when the full CSV is missing, so the container boots cleanly. If
you want full city coverage, drop a real `worldcities.csv` into the geo data directory (see
[Maintenance](maintenance.md#refreshing-geo-data)).

## File uploads hang or error: ClamAV unreachable

**Cause:** the app tries to scan uploads with ClamAV, but you are not running the `antivirus`
profile, so there is no ClamAV daemon to connect to.

**Fix:** disable the scan feature so uploads are marked clean immediately:

```bash
FEATURE_MALWARE_SCAN=False
```

Either run the `antivirus` profile **or** set this flag — don't do neither. (The setup wizard keeps
these aligned; this only bites if you edit `.env` by hand.)

## Telegram linking returns 404 / no Telegram notifications

**Cause:** the `telegram` Compose profile and `FEATURE_TELEGRAM` are out of sync. With
`FEATURE_TELEGRAM=False` the linking endpoints return 404 and Telegram is dropped from
notification delivery; with the profile off there is no bot process to poll updates or send
messages.

**Fix:** pair the two. To use Telegram, run the `telegram` profile **and** leave
`FEATURE_TELEGRAM=True` (the default). To run without it, set `FEATURE_TELEGRAM=False` and leave
the profile off. (As with ClamAV, the setup wizard keeps these aligned; mismatches only happen when
you edit `.env` by hand.)

## Cloudflare 5xx / certificate errors

**Cause:** the Cloudflare proxy ("orange cloud") was enabled before Caddy obtained its certificate,
so the ACME challenge can't reach your origin and TLS issuance loops.

**Fix:** set the records to **DNS only** (grey cloud), let Caddy issue certificates, confirm HTTPS
works, then re-enable the proxy. Full details in [DNS & Cloudflare](dns-cloudflare.md#the-cloudflare-orange-cloud-caveat).

## SSR pages load slowly or buffer / stream incorrectly

**Cause:** Caddy is buffering the SvelteKit SSR streaming response. This happens if a
`flush_interval -1` (or equivalent buffering directive) is set on the reverse proxy to the
frontend.

**Fix:** Caddy must **not** buffer SSR responses — do not set `flush_interval -1` for the frontend
upstream. The shipped Caddyfile variants are already correct; only a hand-edited Caddyfile
reintroduces this. See the buffering warning in the `infra` repo README.

## Media files return 403 / permission errors

**Cause:** Caddy serves media behind HMAC-signed URLs and needs read access to the media volume and
to its own certificate/key material. Wrong file ownership or permissions on the mounted media or
Caddy data volume causes 403s or TLS failures.

**Fix:** ensure the media volume is readable by the Caddy container and that the Caddy data volume
(certificates and keys) is owned by the Caddy process and not world-writable. After fixing
ownership, restart Caddy.

## `web` exits on startup with `oauth.E001` / `oauth.E002`

**Symptom:** after setting `OIDC_SIGNING_KEY_PATH`, the API is down and `web` keeps restarting;
`docker compose logs web` ends in `SystemCheckError` naming `oauth.E001` or `oauth.E002`. The
`telegram` service crash-loops the same way.

**Cause:** both are Error-level system checks, and `web`'s entrypoint runs `migrate` (which runs
the checks) before starting gunicorn, so a half-configured provider stops the process rather than
serving without it:

- **`oauth.E002`** — the key cannot be read. Almost always file permissions: the containers run as
  uid 997, and `generate_oidc_signing_key` writes the key `0600` for your host user. Also check
  the path is the *in-container* one (`/app/certs/oidc.pem`), not the host path, and that the
  service mounts `./certs`.
- **`oauth.E001`** — a key is configured but `OAUTH_ISSUER` is empty.

**Fix:** `chmod 644 certs/oidc.pem` (or set `OAUTH_ISSUER`), confirm with
`docker compose run --rm web python manage.py check`, then `docker compose up -d web
celery_default telegram`. To get the API back immediately instead, comment out
`OIDC_SIGNING_KEY_PATH` and restart: with no key configured the provider is simply off.

## OAuth clients get 403 / error 1010 from `/o/token`

**Cause:** a Cloudflare browser-integrity or bot rule on the API host. OAuth clients call
`/o/token`, `/o/userinfo`, `/o/register` and `/.well-known/*` from servers and CLIs with
non-browser user agents, which such rules block.

**Fix:** add a Cloudflare WAF skip rule for those paths on the API hostname (or disable Browser
Integrity Check for it). Verify with `curl -i -X POST https://<api-host>/o/token`: expect a `400`
JSON error from Revel, not a Cloudflare page.

## The stack won't start after editing `.env`

**Cause:** common `.env` mistakes:

- **Unquoted values with special characters.** Values containing spaces, `#`, or shell
  metacharacters must be quoted: `SECRET_KEY="abc#def ghi"`.
- **Missing `SECRET_KEY` / `SALT_KEY`.** Both are required; Django and the signing utilities fail
  to boot without them. The wizard generates them — if you regenerated `.env` by hand, make sure
  both are present and non-empty.
- **A feature flag set without quotes or with a non-boolean value.** Flags are cast to bool; use
  exactly `True` or `False`.

**Fix:** review the offending lines, quote values as needed, ensure the required secrets are
present, and bring the stack back up with `docker compose up -d`. Check `docker compose logs web`
for the specific error.
