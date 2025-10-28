# Revel

**An open-source, community-focused event management platform.**

<!-- Status -->
[![Status](https://img.shields.io/badge/status-Beta-orange?style=for-the-badge)](https://github.com/letsrevel/revel)
[![License](https://img.shields.io/badge/license-MIT-blue?style=for-the-badge)](./LICENSE)
![Django](https://img.shields.io/badge/django-5.2+-092E20.svg?logo=django&logoColor=white&style=for-the-badge)

<!-- Tooling / meta -->
![Python](https://img.shields.io/badge/python-3.13%2B-3776AB.svg?logo=python&logoColor=white)
![Ruff](https://img.shields.io/badge/lint-ruff-46aef7?logo=ruff&logoColor=white)
![mypy strict](https://img.shields.io/badge/types-mypy-informational.svg)

<!-- CI -->
[![Test](https://github.com/letsrevel/revel-backend/actions/workflows/test.yaml/badge.svg)](https://github.com/letsrevel/revel-backend/actions/workflows/test.yaml)
[![Build](https://github.com/letsrevel/revel-backend/actions/workflows/build.yaml/badge.svg)](https://github.com/letsrevel/revel-backend/actions/workflows/tests.yaml)



Revel is an event management and ticketing platform designed with community at its heart. Initially created to serve the specific needs of queer, LGBTQ+, and sex-positive communities, it is built to be event-agnostic, scalable, and a powerful tool for any group that values privacy, control, and transparency.

Unlike monolithic, corporate platforms that treat events as transactions, Revel treats them as part of a larger community ecosystem.

---

### ✨ Live Demo (Alpha)

You can try out an early version of Revel yourself at https://demo.letsrevel.io

The api lives at https://demo-api.letsrevel.io/api/docs

**NOTE:** Emails are dry, and the data is reset every day at 00:00 CET.

More info on what's available [here](./src/events/management/commands/README.md).

---

## 🤔 Why Revel? The Philosophy

Revel is being built to address the shortcomings of existing event platforms, especially for communities that prioritize safety, autonomy, and trust.

*   **For Communities, Not Corporations:** Mainstream platforms often have restrictive content policies or a lack of privacy features, creating challenges for adult, queer, or activist-oriented events. Revel is explicitly designed to support these communities.
*   **Open, Transparent & Self-Hostable:** Avoid vendor lock-in. You can host Revel on your own infrastructure for free, giving you complete control over your data and eliminating platform commissions. Its open-source nature means you can trust the code you run.
*   **Fair & Simple Pricing:** For those who choose our future hosted version, the model is simple: **no charge for free events or events where you handle payments yourself**; a **3% + 0.50 cents commission** on paid tickets sold and bought through Revel. This significantly undercuts the high fees of major platforms and helps us keep the platform online, free and open source.

## 🚀 Key Features

Revel combines the ticketing power of platforms like Eventbrite with the community-building tools of Meetup, all under a privacy-minded, open-source framework.

#### Community & Membership
*   **Organizations:** Create and manage your community's central hub. Customize its visibility (Public, Private, Members-Only).
*   **Roles & Permissions:** Assign roles like Owner, Staff, and Member, with a granular permission system to control who can create events, manage members, and more.
*   **Membership System:** Manage a roster of members, enabling members-only events and fostering a sense of belonging.

#### Trust, Safety & Privacy
*   **Advanced Attendee Screening:** Gate event eligibility with custom questionnaires. Automatically review submissions or use a manual/hybrid approach to ensure attendees align with your community's values.
*   **Full Data Ownership:** When self-hosting, you control your data. No third-party trackers, no selling of event data. Keep your community's information safe.
*   **Tailored Invitations:** Send direct invitations that can waive specific requirements (like questionnaires, membership or purchase) for trusted guests.

#### Core Event & Ticketing Features
*   **Event & Series Management:** Easily create single events or recurring event series under your organization.
*   **Ticketing & RSVPs:** Support for both paid/free ticketed events (powered by Stripe) and simpler RSVP-based gatherings.
*   **QR Code Check-In:** Manage event entry smoothly with QR code tickets and a staff-facing check-in flow.
*   **Potluck Coordination:** A unique, built-in system for attendees to coordinate bringing items, moving logistics off messy spreadsheets.

---

## 💻 Tech Stack

Revel is built with a modern and robust backend, designed for performance and scalability.

*   **🐍 Backend:** Python 3.13+ with **Django 5+**
*   **🚀 API:** **Django Ninja** for a fast, modern, and auto-documenting REST API.
*   **🐘 Database:** **PostgreSQL** with **PostGIS** for powerful geo-features.
*   **⚙️ Async Tasks:** **Celery** with **Redis** for background jobs (emails, evaluations).
*   **🐳 Deployment:** Fully containerized with **Docker** for easy setup and deployment.

---

## 🏁 Quick Start (Development)

Get a local development environment running in minutes. You'll need `make`, `Docker`, and Python 3.12+.

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/letsrevel/revel-backend.git
    cd revel-backend
    ```
    
2.  **Make sure you have the necessary geo data:**
    *   You must download [IP2LOCATION-LITE-DB5.BIN](https://lite.ip2location.com/database/db5-ip-country-region-city-latitude-longitude?lang=en_US) and place it in `src/geo/data/`
    *   You must download the [worldcities.csv](https://simplemaps.com/data/world-cities) and place it in `src/geo/data/` (or for dev purposes just copy `worldcities.mini.csv` into `worldcities.csv`)


3.  **Run the setup command:**
    This command fully automates the setup process.
    ```bash
    make setup
    ```

4.  **You're ready!**
    *   The API is running at `http://localhost:8000`
    *   Interactive API docs (Swagger UI) are at `http://localhost:8000/api/docs`
    *   A default superuser is created (`admin@letsrevel.io` / `password`).

---

## 🛠️ Development Commands

The project uses a `Makefile` to streamline common development tasks.

| Command              | Description                                                      |
| -------------------- | ---------------------------------------------------------------- |
| `make setup`         | Runs the complete one-time setup for the dev environment.        |
| `make run`           | Starts the Django development server.                            |
| `make check`         | Runs all checks: formatting, linting, and type checking.         |
| `make test`          | Runs the full `pytest` test suite and generates a coverage report. |
| `make run-celery`      | Starts the Celery worker for processing background tasks.        |
| `make run-celery-beat` | Starts the Celery beat scheduler for periodic tasks.             |
| `make migrations`    | Creates new database migrations based on model changes.          |
| `make migrate`       | Applies pending database migrations.                             |
| `make shell`         | Opens the Django shell.                                          |
| `make restart`       | Restarts the Docker environment and recreates the database.      |
| `make nuke-db`       | **Deletes** the database and all migration files. Use with caution. |

---

## 📂 Project Structure

The codebase is organized into a `src` directory with a clear separation of concerns, following modern Django best practices.

*   `src/revel/`: The core Django project settings.
*   `src/accounts/`: User authentication, registration, and profile management.
*   `src/events/`: The core logic for organizations, events, tickets, and memberships.
*   `src/questionnaires/`: The questionnaire building, submission, and evaluation system. [📖 Read more](src/questionnaires/README.md)
*   `src/geo/`: Geolocation features (cities, IP lookups).
*   `src/telegram/`: Integration with the Telegram Bot API (note: this is a broken early prototype).
*   `src/api/`: Main API configuration, exception handlers, and global endpoints.

Each app contains a `controllers/` directory for API endpoints and a `service/` directory for business logic.

---

## 🤝 Contributing

We welcome contributions! Please read our **[CONTRIBUTING.md](CONTRIBUTING.md)** to learn how you can get involved, from reporting bugs to submitting code.

---

## 📜 License

This project is licensed under the MIT license. See [LICENSE](LICENSE).

## Acknowledgements
- Revel uses the IP2Location LITE database for <a href="https://lite.ip2location.com">IP geolocation</a>.
- Revel uses the [World Cities Database](https://simplemaps.com/data/world-cities) from SimpleMaps, available under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).