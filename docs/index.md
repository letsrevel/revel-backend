# Revel Backend

**An open-source, community-focused event management platform.**

Revel is an event management and ticketing platform designed with community at its heart. Built for groups that value privacy, control, and transparency -- from queer and LGBTQ+ communities to activist collectives and beyond.

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **Getting Started**

    ---

    Set up your development environment in minutes and start contributing.

    [:octicons-arrow-right-24: Quick Start](getting-started/quickstart.md)

-   :material-sitemap:{ .lg .middle } **Architecture**

    ---

    Understand the service layer, controller patterns, and core design decisions.

    [:octicons-arrow-right-24: Architecture Overview](architecture/index.md)

-   :material-book-open-variant:{ .lg .middle } **Guides**

    ---

    Deep dives into user flows, observability, i18n, and more.

    [:octicons-arrow-right-24: Explore Guides](guides/index.md)

-   :material-source-pull:{ .lg .middle } **Contributing**

    ---

    Code style, testing patterns, and how to submit your first PR.

    [:octicons-arrow-right-24: Contribute](contributing/index.md)

-   :material-scale-balance:{ .lg .middle } **Architecture Decision Records**

    ---

    Why we chose HMAC over S3, Django Ninja over DRF, UV over pip, and more.

    [:octicons-arrow-right-24: Browse ADRs](adr/index.md)

</div>

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Backend** | Python 3.13+ / Django 5.2 LTS |
| **API** | Django Ninja + Django Ninja Extra |
| **Database** | PostgreSQL with PostGIS |
| **Async Tasks** | Celery with Redis |
| **Auth** | JWT (custom user model) |
| **Observability** | Loki, Grafana, Tempo, Prometheus, Pyroscope |
| **Deployment** | Docker / Docker Compose |
| **Deps** | UV (never pip) |

## Related Repositories

| Repository | Description |
|-----------|-------------|
| [revel-backend](https://github.com/letsrevel/revel-backend) | Django REST API, business logic, database models (this repo) |
| [revel-frontend](https://github.com/letsrevel/revel-frontend) | SvelteKit web application, user interface |
| [infra](https://github.com/letsrevel/infra) | Docker Compose, reverse proxy, observability, deployment |
