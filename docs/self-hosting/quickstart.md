# Quickstart

This guide takes you from a fresh server to a running Revel instance using the interactive setup
wizard shipped in the `infra` repository. The wizard does the heavy lifting: it asks a handful of
questions, writes your `.env`, picks the right Caddy configuration, fetches geo data, and brings
the stack up.

!!! note "Prerequisites"
    A Linux host with Docker and the Docker Compose plugin installed, a domain you control, and
    ports 80/443 reachable from the internet (for TLS certificate issuance). The Slim tier runs
    comfortably on a 2 vCPU / 4 GB VPS.

## 1. Clone the infra repository

```bash
git clone https://github.com/letsrevel/infra && cd infra
```

The `infra` repo contains the Compose files, Caddyfile variants, and the `setup.sh` wizard. The
application images themselves are pulled from the container registry — you do not need to clone
the backend or frontend repos to run an instance.

## 2. Run the setup wizard

```bash
./setup.sh
```

The wizard first checks for Docker (offering to install it), the Compose plugin, `openssl` and free
ports 80/443, and backs up any existing `.env`. It then prompts you for:

1. **Tier** — Slim or Full, recommended from the detected hardware. The tier is only a preset: it
   sets the suggested resource limits and the default answer for observability and antivirus. Every
   optional service below is still toggled individually.
2. **Domains** — your frontend domain and API domain.
3. **Email** — real SMTP (host, port, username, password, from-address) or console/dry-run
   (`EMAIL_DRY_RUN=True`) for a test instance.
4. **Optional services** — each service answer drives both its Compose profile and its `FEATURE_*`
   flag; single-org mode is a feature flag only (no Compose profile):
    - the **observability stack** (default yes on Full), and if enabled, the **Grafana domain**;
    - **ClamAV malware scanning** (default yes on Full);
    - the **Telegram bot** (asks for the bot token);
    - **LLM questionnaire evaluation** (asks for a generic LLM model, e.g. `openai/gpt-4o-mini`, and
      an API key);
    - **single-org mode** (default yes; sets `FEATURE_ORGANIZATION_CREATION=False`);
    - **Stripe payments** (secret key, platform account id, default currency).
5. **Google login (optional)** — one Google OAuth client, used for user-facing login (written as
   `OIDC_PROVIDERS=google` plus `OIDC_GOOGLE_*`), for the Django admin login (`GOOGLE_SSO_*`, with
   an allow-list of admin emails and an optional SSO-only admin), or both.
6. **Login canary** — only asked when observability is enabled; needs a dedicated account with no
   org/staff roles and 2FA disabled.
7. **Cloudflare** — whether you proxy through Cloudflare (orange cloud), which picks the Caddyfile
   variant.
8. **Resource limits** — the tier's suggested CPU, memory, gunicorn, Celery and Postgres values,
   which you can review and override one by one.
9. **Geo data** — the base URL to download the cities dataset from, and whether to download the
   IP2Location LITE database.

It generates `SECRET_KEY`, `SALT_KEY`, the database password and the Grafana admin password for
you. Apple Wallet and Google Wallet are not prompted: set the `APPLE_WALLET_*` / `GOOGLE_WALLET_*`
variables in `.env` by hand if you want wallet passes.

When it finishes, the wizard:

- writes a complete `.env` file (review it before going to production),
- selects the appropriate **Caddyfile variant** (the generic edge variant, or the Cloudflare
  variant if you proxy through Cloudflare — see [DNS & Cloudflare](dns-cloudflare.md)),
- **fetches the geo data** (the cities dataset, and optionally the IP2Location BIN; if the full
  cities CSV is unavailable it falls back to the bundled 50-city mini dataset), and
- runs `docker compose up -d` to start the stack.

!!! note "Keep this page in sync"
    The exact prompts are defined by `infra/setup.sh`. If the wizard changes, treat the script as
    the source of truth and update this list to match.

## 3. Create the admin and first organization

Once the containers are healthy, the wizard offers to run the first-run bootstrap for you. It
prompts for an admin email, password, and organization name (proposing a slug), then creates the
superuser and the first organization and prints your admin-panel and frontend URLs. You can also
run it manually at any time:

```bash
docker compose exec web python manage.py bootstrap_admin
```

The command pins the superuser's username to its email — matching every account created through the
public API — so prefer it over the stock `createsuperuser`, which lets the two diverge.

If you answered yes to single-org mode, the wizard set `FEATURE_ORGANIZATION_CREATION=False`, so
regular users cannot create their own organizations — your superuser remains able to create more.
See [Tiers & Configuration](tiers.md#single-org-instances).

## 4. Visit your instance

With DNS pointed at the box and certificates issued, your instance is reachable at:

- **Frontend** — `https://<FRONTEND_DOMAIN>`
- **API** — `https://<API_DOMAIN>` (interactive docs at `https://<API_DOMAIN>/api/docs`)
- **Grafana** (only if you enabled observability) — `https://<GRAFANA_DOMAIN>`

A quick sanity check on your configuration: `GET https://<API_DOMAIN>/version` returns the
active feature flags (`organization_creation`, `telegram`, `llm_evaluation`) and the configured
`sso_providers`, so you can confirm the deployment picked up the settings you set.

If the site does not come up, the most common cause is TLS issuance failing behind Cloudflare's
proxy — see the [DNS & Cloudflare](dns-cloudflare.md) caveat and the
[Troubleshooting](troubleshooting.md) page.
