# Keycloak realm for the e2e stack

`revel-realm.json` is imported by the `keycloak` service in `docker-compose-e2e.yml`
(`start-dev --import-realm`) every time the container starts. It exists only for the
local end-to-end suite (`make e2e-*`, driven from `revel-frontend`); `make run` and
`make test` never start Keycloak. The client secret and passwords are deliberately trivial.

| What | Value |
|---|---|
| Issuer | `http://localhost:8080/realms/revel` |
| Client | `revel-backend` / secret `e2e-secret`, redirect URI `http://localhost:8000/api/auth/oidc/keycloak/callback` |
| Admin console | `http://localhost:8080` — `admin` / `admin` (also the admin REST API, for per-run throwaway users) |
| Backend env | set inline by the e2e Make targets (`E2E_OIDC_ENV`): `OIDC_PROVIDERS=keycloak`, `OIDC_KEYCLOAK_*` |

Seeded users (password `password123`):

| User | Purpose |
|---|---|
| `charlie.member@example.com` | Mirrors the `make bootstrap` persona: auto-links to the existing Revel account; unlink allowed (has a password) |
| `sso.only@example.com` | Not a Revel user: first login creates the account; unlink refused until a password is set |
| `unverified.sso@example.com` | `emailVerified: false`: Revel refuses with `/login?error=oidc_unverified_email` |

`verifyEmail` is off in the realm so the unverified user can log in at Keycloak and reach
Revel's own refusal. Keycloak rejects unknown keys in the realm file, so keep notes here,
not in the JSON.
