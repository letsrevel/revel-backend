# Self-Hosting Revel

Revel is designed to run as a managed SaaS, but the entire stack is open and self-hostable.
You can run a complete instance — frontend, API, background workers, database, and (optionally)
the full observability suite — on a single box with Docker Compose. This section walks you
through standing one up, sizing it, and keeping it healthy.

Self-hosting is a good fit if you want full data ownership, you run a single community or
organization, or you simply want to learn how the platform fits together. It is *not* a good
fit if you need the operational guarantees, backups, and uptime of a managed service — for
that, use the hosted instance.

## Two tiers

Revel scales down a long way. There are two reference sizings:

- **Slim** — roughly **2 vCPU / 4 GB RAM**, about €20/month (Hetzner CPX22, September 2026). Runs the core services
  only, with conservative resource limits. ClamAV, Telegram, and the observability stack are
  switched off. This is the recommended starting point for a single-org instance.
- **Full** — **8 vCPU / 32 GB RAM**. Can run every optional profile: antivirus scanning, the LGTM
  observability stack (Grafana/Loki/Tempo/etc.), the Telegram bot, and the canary.
  This mirrors a production deployment.

The difference between the two is almost entirely a matter of which Compose profiles you enable
and a handful of resource-limit environment variables — see
[Tiers & Configuration](tiers.md).

## Architecture recap

A minimal (Slim) instance runs these core services:

- **caddy** — TLS termination, reverse proxy, and HMAC-protected media serving.
- **frontend** — the SvelteKit web app (SSR + CSR).
- **web** — the Django/Ninja API (served by Gunicorn).
- **celery** — background task worker.
- **beat** — the Celery scheduler for periodic jobs.
- **postgres** — PostgreSQL with PostGIS.
- **pgbouncer** — connection pooler in front of Postgres.
- **redis** — Celery broker, result backend, and cache.

Everything else (ClamAV, the LGTM observability stack, the Telegram bot) is optional and
gated behind Compose profiles and feature flags.

## Optional dependencies

Revel degrades gracefully when external integrations are absent. The table below summarizes what
each dependency does, whether you can omit it, and the lever you use to do so:

| Dependency | Safe to omit? | Behaviour when absent | Lever |
| --- | --- | --- | --- |
| ClamAV | Yes | Uploads marked clean, no scan | `FEATURE_MALWARE_SCAN=False` |
| Stripe | Yes (unless selling) | Only paid checkout hits it | leave keys as placeholders |
| LLM / OpenAI | Yes | Manual questionnaire eval still works | `FEATURE_LLM_EVALUATION=False` |
| Geo / IP2Location | Yes | Lookups return `None` | BIN optional; mini cities fallback |
| Telegram | Yes | Per-user delivery skipped | `FEATURE_TELEGRAM=False`; profile off |
| OIDC login (Google, Keycloak, …) | Yes (opt-in) | Login buttons hidden; password auth only | `OIDC_PROVIDERS=google` + per-provider vars |
| OAuth / OIDC provider (third-party apps, MCP hosts, "Sign in with") | Yes (opt-in) | Every `/o/*`, `/.well-known/*` and `/api/oauth/*` route is `404` | `OIDC_SIGNING_KEY_PATH` + `OAUTH_ISSUER` |
| Eventbrite listing sync | Yes (opt-in) | Integrations panel hides the provider | `INTEGRATIONS_EVENTBRITE_CLIENT_ID` + `_CLIENT_SECRET` |
| Apple Wallet | Yes | `/wallet/apple` → 503; PDF tickets fine | leave certs unset |
| Google Wallet | Yes | `/wallet/google` → 503; PDF tickets fine | leave issuer ID / key unset |
| Email / SMTP | Required* | Needed for verification | real SMTP or `EMAIL_DRY_RUN=True` |

!!! note "Email is effectively required"
    Email is the one "soft requirement": account verification, password resets, and ticket
    delivery all flow through it. For a real instance, configure a working SMTP provider. For a
    throwaway test instance you can set `EMAIL_DRY_RUN=True`, which logs messages instead of
    sending them.

## Google Wallet setup (one-time)

1. Create an issuer account in the [Google Pay & Wallet Console](https://pay.google.com/business/console)
   (requires business verification — can take days) and accept the Google Wallet API ToS to get your
   **Issuer ID**.
2. Create a GCP service account, enable the Google Wallet API in its project, and add the service
   account's email under **Users** in the Wallet Console.
3. Provision the service-account JSON key to the backend host and set:
   `GOOGLE_WALLET_ISSUER_ID`, `GOOGLE_WALLET_SERVICE_ACCOUNT_KEY_PATH` (and optionally
   `GOOGLE_WALLET_CLASS_PREFIX`, default `revel`).
4. Passes show **[TEST ONLY]** until you request publishing access in the Wallet Console.

If your GCP org enforces `iam.disableServiceAccountKeyCreation`, creating the key requires a
temporary project-scoped org-policy override (allow ~2 minutes of propagation before retrying).

## OAuth / OpenID Connect provider setup (one-time)

Revel can act as an OAuth 2.1 authorization server and OpenID Provider, so third-party apps,
scripts and MCP hosts can call the API on a user's behalf with scoped, revocable tokens. It is
**off by default** and switched on by credential presence ([ADR-0018](../adr/0018-oauth-oidc-provider.md)):
the provider exists only when a readable signing key *and* an issuer are configured. The infra
setup wizard can do steps 1–2 for you.

1. **Generate the signing key** (RSA 2048, unencrypted PKCS8 PEM) in the infra directory:

    ```bash
    openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out certs/oidc.pem
    chmod 644 certs/oidc.pem
    ```

    Inside a running stack, `docker compose exec web python manage.py generate_oidc_signing_key
    --out /app/certs/oidc.pem` produces the same format. The `chmod 644` is not optional: the
    command writes the file `0600` for your host user, but the containers run as uid 997. An
    unreadable key **stops `web` from starting**: its entrypoint runs `migrate`, which fails the
    `oauth.E002` system check (see
    [Troubleshooting](troubleshooting.md#web-exits-on-startup-with-oauthe001-oauthe002)).

2. **Set in `.env`** (`./certs` is mounted read-only at `/app/certs` in every service that
   runs management commands: `web`, `celery_default` and `telegram`):

    ```bash
    OIDC_SIGNING_KEY_PATH=/app/certs/oidc.pem
    OAUTH_ISSUER=https://api.example.org   # your public API origin, no trailing slash
    ```

    `OAUTH_ISSUER` must be exactly the origin that serves `/.well-known/openid-configuration`;
    clients reject tokens whose `iss` does not match it.

3. **Restart and verify:**

    ```bash
    docker compose up -d web celery_default telegram
    docker compose exec web python manage.py check      # no oauth.E001 / oauth.E002
    curl -s https://api.example.org/api/version          # features.oauth_provider: true
    curl -s https://api.example.org/.well-known/openid-configuration   # "issuer" equals OAUTH_ISSUER
    ```

4. **Back up `certs/oidc.pem`** off the box: it is not in the database dump (see
   [Maintenance](maintenance.md#oauth-signing-key)).

No Caddy change is needed: the API host already proxies `/o/*` and `/.well-known/*` to Django.
The consent page and the **Developer apps** / **Connected apps** screens ship with the frontend,
so deploy a frontend that includes them. Turning the provider off again is config-only: unset
`OIDC_SIGNING_KEY_PATH` and restart; every provider route returns to `404` and no data is lost.

Optional tuning (defaults shown): `OAUTH_MAX_APPS_PER_USER=10` (developer-portal apps per user),
`OAUTH_DCR_DAILY_CAP=500` (instance-wide dynamic client registrations per day),
`OAUTH_DCR_UNUSED_TTL_HOURS=24` (how long a dynamically registered app that never completed an
authorization is kept). The token endpoints are rate-limited to 60 requests/min per IP and
dynamic registration to 10/hour per IP; a busy "Sign in with" integration or a hosted MCP host
calls from a single server IP, so watch for `429 slow_down` as usage grows. For app developers,
see the [OAuth developer guide](../developer-guide/oauth.md).

## Where to next

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Quickstart**

    ---

    Clone the infra repo, run the setup wizard, and be live in minutes.

    [:octicons-arrow-right-24: Quickstart](quickstart.md)

-   :material-dns:{ .lg .middle } **DNS & Cloudflare**

    ---

    The records you need, and the Cloudflare cert-issuance caveat.

    [:octicons-arrow-right-24: DNS & Cloudflare](dns-cloudflare.md)

-   :material-tune:{ .lg .middle } **Tiers & Configuration**

    ---

    Slim vs full, Compose profiles, and every environment knob.

    [:octicons-arrow-right-24: Tiers & Configuration](tiers.md)

-   :material-chart-line:{ .lg .middle } **Observability**

    ---

    Enable the LGTM stack when you want dashboards and traces.

    [:octicons-arrow-right-24: Observability](observability.md)

-   :material-wrench:{ .lg .middle } **Maintenance**

    ---

    Backups, upgrades, geo refresh, and dataset attribution.

    [:octicons-arrow-right-24: Maintenance](maintenance.md)

-   :material-alert-circle:{ .lg .middle } **Troubleshooting**

    ---

    The known failure modes and their fixes.

    [:octicons-arrow-right-24: Troubleshooting](troubleshooting.md)

</div>
