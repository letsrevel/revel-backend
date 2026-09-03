# ADR-0016: Generic OIDC login, hand-rolled on PyJWT + httpx

## Status

Accepted

## Context

Login previously supported password-based authentication (+ TOTP) and a Google-only endpoint that verified a posted
Google ID token via `django-google-sso`/`google-auth`. Self-hosters asked for their own
identity providers (Keycloak, Authentik, institutional IdPs), and the Google path was
never wired into the frontend.

Options considered:

1. **django-allauth** (`socialaccount` openid_connect + headless) — complete, but owns
   signup, verification and password reset, which collide with Revel's guest accounts,
   custom verification tokens, TOTP and global bans. An accounts rewrite, not an addition.
2. **mozilla-django-oidc** — healthy, MPL-2.0, but built around Django session auth
   and a single provider; fights the JWT + SvelteKit-proxy architecture.
3. **authlib** — clean, BSD, but its Django client expects session state and adds a
   dependency to replace ~200 lines that already existed for Google.
4. **Thin client on PyJWT + httpx** (already installed) — the same surface the Google
   path had, generalised: discovery, PKCE, state/nonce, JWKS verification.

## Decision

Option 4. `accounts/service/oidc.py` implements authorization-code + PKCE with a
**backend-handled callback**: the browser only ever receives redirects from the API, the
client secret and PKCE verifier stay server-side, and the last leg reuses the
impersonation pattern (a 60-second single-use JWT the frontend exchanges for the normal
token pair). Providers are env-configured (`OIDC_PROVIDERS`, `OIDC_<KEY>_*`) per
ADR-0008. Identities live in `ExternalIdentity(provider, subject)`.

Policies:
- Accounts may hold a password **and** identities; the former SSO lockouts are gone.
- Auto-link to an existing account by email only when the ID token asserts
  `email_verified: true`; otherwise refuse.
- TOTP is skipped for IdP logins (the IdP's MFA is trusted).
- `is_staff`/`is_superuser` are never derived from claims.
- `django-google-sso` remains only for the Django admin login page.
- `state` is also bound to the browser via an HttpOnly, `SameSite=Lax` cookie on the API
  origin.

## Consequences

- Any OIDC issuer works with three env vars; Google is one of them.
- ~350 lines of security-relevant code we own (state, nonce, PKCE, token validation).
  Mitigated by a fixed alg allowlist, JWKS-by-kid via `PyJWKClient`, single-use state,
  and the test matrix in `accounts/tests/test_oidc_*.py`.
- Revel as an OIDC *Provider*, per-organization SSO, Apple, and plain-OAuth2 providers
  (GitHub/Discord) are out of scope; the env config has a documented upgrade path to a
  provider model.
