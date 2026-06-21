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

- **Slim** — roughly **2 vCPU / 4 GB RAM (~5 €/mo)** on a small VPS. Runs the core services
  only, with conservative resource limits. ClamAV, Telegram, and the observability stack are
  switched off. This is the recommended starting point for a single-org instance.
- **Full** — **8 vCPU / 32 GB RAM**. Runs every optional profile: antivirus scanning, the LGTM
  observability stack (Grafana/Loki/Tempo/etc.), Flower, the Telegram bot, and the canary.
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

Everything else (ClamAV, the LGTM observability stack, Flower, the Telegram bot) is optional and
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
| Apple Wallet | Yes | `/wallet/apple` → 503; PDF tickets fine | leave certs unset |
| Email / SMTP | Required* | Needed for verification | real SMTP or `EMAIL_DRY_RUN=True` |

!!! note "Email is effectively required"
    Email is the one "soft requirement": account verification, password resets, and ticket
    delivery all flow through it. For a real instance, configure a working SMTP provider. For a
    throwaway test instance you can set `EMAIL_DRY_RUN=True`, which logs messages instead of
    sending them.

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
