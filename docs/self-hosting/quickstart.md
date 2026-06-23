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

The wizard prompts you for:

1. **Tier** — Slim or Full. This selects the `COMPOSE_PROFILES` and resource limits. Start with
   Slim unless you specifically want the observability stack and antivirus.
2. **Domains** — your `FRONTEND_DOMAIN` and `API_DOMAIN` (plus `grafana` and `docs`
   subdomains if you chose Full).
3. **Email** — SMTP host, port, credentials, and the from-address used for verification and
   ticket delivery. (You can opt into `EMAIL_DRY_RUN=True` for a test instance.)
4. **Optional integrations** — whether to enable Stripe (paid events), Telegram, the LLM
   questionnaire evaluator, and Apple Wallet. Anything you skip is left disabled via feature
   flags, and the corresponding service is left out of the Compose profiles.
5. **Secrets** — it generates `SECRET_KEY`, `SALT_KEY`, and database/Redis passwords for you, and
   collects any third-party keys (Stripe, OpenAI) you opted into.

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

On a single-org instance you will typically also set `FEATURE_ORGANIZATION_CREATION=False` so that
regular users cannot create their own organizations — your superuser remains able to create more.
See [Tiers & Configuration](tiers.md#single-org-instances).

## 4. Visit your instance

With DNS pointed at the box and certificates issued, your instance is reachable at:

- **Frontend** — `https://<FRONTEND_DOMAIN>`
- **API** — `https://<API_DOMAIN>` (interactive docs at `https://<API_DOMAIN>/api/docs`)
- **Grafana** (Full tier only) — `https://grafana.<your-domain>`

If the site does not come up, the most common cause is TLS issuance failing behind Cloudflare's
proxy — see the [DNS & Cloudflare](dns-cloudflare.md) caveat and the
[Troubleshooting](troubleshooting.md) page.
