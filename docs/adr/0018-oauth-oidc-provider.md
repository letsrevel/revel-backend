# ADR-0018: OAuth 2.1 / OIDC provider on django-oauth-toolkit

## Status

Accepted (2026-09-15)

## Context

[ADR-0016](0016-oidc-relying-party.md) made Revel an OIDC *relying party* and explicitly
excluded the *provider* role. Three things then asked for it at once: organizers wanting their
own scripts and Zapier-style connectors to talk to the API without handing over a password;
third-party sites wanting "Sign in with Revel"; and MCP hosts (Claude and friends) wanting to
act on a user's behalf. The alternative to a real authorization server is organizer-owned API
keys, which are long-lived bearer secrets with no consent step, no scope, and no revocation
story beyond "rotate and hope".

The MCP authorization profile in particular constrains the choice of library. It requires
**RFC 8707 resource indicators** (so a token is bound to the API it was issued for), **RFC 9728
protected-resource metadata** (so a client that gets a 401 can discover where to get a token),
and **RFC 7591 dynamic client registration** (so a host can register itself at runtime, with no
human in the loop).

### Library evidence (gathered 2026-09-14)

| | django-oauth-toolkit 3.4.1 | Authlib 1.8.0 |
|---|---|---|
| Stars / forks | 3.3k / 856 | 5.4k / 563 |
| Commits, last 6 months | 100 | 76 |
| Contributors with 10+ commits | 14 | 2 |
| Open issues | 46 | 144 |
| Releases in 2026 | 3.2, 3.3, 3.4.0, 3.4.1 | 1.6.11 … 1.8.0 |
| Python 3.14 / Django 5.2 classifiers | yes / yes (+6.0) | yes / not declared |
| RFC 7591/7592 dynamic client registration | yes | yes |
| RFC 8707 resource indicators | yes | **no** |
| RFC 9728 protected-resource metadata | yes | **no** |
| RFC 9700 BCP toggles, refresh-token families | yes | partial |
| Type hints | no | no |
| Licence / deps | BSD-2; oauthlib (BSD), jwcrypto (LGPL-3, no obligation for a hosted service), requests | BSD-3; joserfc |

Authlib is the more popular package and lacks two of the three RFCs the profile requires. DOT
was quiet from 2024-09 to 2025-11 and then shipped four minors tracking exactly that profile —
that is the roadmap this feature has to follow.

## Decision

Revel is a full OAuth 2.1 authorization server and OpenID Provider, built on
**django-oauth-toolkit 3.4.x**. Hand-rolling was rejected: it is six endpoints of
spec-critical checks (PKCE, redirect-URI matching, refresh families, audience binding) whose
bugs are silent.

- **Tokens are user-delegated.** An app's effective power is *the scopes the user granted* ∩
  *what that user may do right now* through the existing organization permission map. An app
  as its own principal ("organization installation") is a documented upgrade path, not v1.
- **Coarse public scopes resolve internally to `PermissionKey`s** (`oauth/scopes.py`). The
  public contract stays readable; enforcement stays exact. A scope label must never promise
  less than its keys grant, which is why `org:events` names deletion and `org:tickets` names
  discount codes, seating, refunds and revenue.
- **Consent is headless.** The SvelteKit frontend renders the screen; the backend validates and
  issues through DOT's `OAuthLibCore` (`/api/oauth/authorize`, GET to describe and POST to
  decide). DOT's session-based `AuthorizationView` is not mounted. The decision is bound to the
  screen by a signed, short-lived consent ticket, so "the user saw what they granted" is the
  backend's own guarantee rather than a frontend-only one.
- **Access tokens are opaque and hashed at rest**
  (`COMPLIANT_BCP_RFC9700_TOKEN_STORAGE=True`), refresh tokens rotate with reuse protection and
  a zero grace period. **Introspection is not mounted**: any client-authenticated caller —
  including a self-registered DCR client — could otherwise introspect any user's token, and no
  separate resource server needs it.
- **Protocol endpoints are DOT's own views**, mounted under `/o/` in the `oauth2_provider`
  URL namespace (DOT's metadata and DCR views `reverse()` their own URL names), with the three
  discovery documents at the API origin root. Our routes live under `/api/oauth/`.
- **The provider is feature-flagged on credential presence** (ADR-0008): it exists iff
  `OIDC_SIGNING_KEY_PATH` points at an RSA private key PEM. Unset, nothing is mounted, every
  protocol route 404s, and nothing else in the API changes.
- **Reaching a new surface with an app token requires an explicit auth-class change.** Only
  controllers switched to `ScopedJWTAuth` accept app tokens; everything on `I18nJWTAuth` or
  `OptionalAuth` refuses them by construction. The switch list is therefore an allow-list, and
  a convention test (`oauth/tests/test_scope_coverage.py`) fails when it widens without review.
- **`OAUTH2_PROVIDER_APPLICATION_MODEL` and the whole `OAUTH2_PROVIDER` dict live in
  `src/revel/settings/oauth.py`**, not in `base.py`.

## Consequences

- **DOT is untyped**, so every subclass needs `# type: ignore[misc]` under
  `disallow_subclassing_any`: the swapped `Application`, the `OAuth2Validator`, the scopes
  backend, the two metadata views and the five admin classes. One `[[tool.mypy.overrides]]`
  block gives `oauth2_provider.*`, `oauthlib.*` and `jwcrypto.*` `ignore_missing_imports`; our
  wrapper layer casts at the boundary.
- **`oauth/migrations/0001_initial.py` carries a hand-written
  `run_before = [("oauth2_provider", "0001_initial")]`** — a documented exception to "never
  hand-edit schema migrations", because DOT's own initial migration has no dependency on the
  swapped application model and would otherwise build its FKs first. It must be re-added after
  any regeneration (`make nuke-db`, `make restart`) and is guarded by
  `oauth/tests/test_migrations.py`. See
  [engineering-notes](../engineering-notes.md#oauth0001-run_before-a-documented-hand-edit).
- **`jwcrypto` now sits beside `PyJWT`** in the dependency tree: DOT signs ID tokens and serves
  JWKS through jwcrypto, while ADR-0016's relying-party code and our own session JWTs use
  PyJWT. Two JOSE implementations is redundancy we accept rather than fork DOT over; jwcrypto
  is LGPL-3, which carries no distribution obligation for a hosted service (ADR-0010).
- **Banning a user now reaches two token worlds.** `blacklist_user_tokens` revokes DOT tokens as
  well as ninja-jwt ones, unconditionally — including on email rotation, so changing an email
  address disconnects every connected app. A *global ban* additionally deactivates the OAuth
  applications the banned user owns and revokes their tokens, which disconnects every user of
  such an app. Both are intended; `is_active` is reversible.
- **The admin's credential views are readonly by construction.** Under hashed-token storage the
  `token_checksum` column is the sole bearer verifier, so an editable change form is staff
  impersonation; all four DOT token admins render every field readonly and keep only the
  revoke action.
- **Authorize errors come back as `400 {"detail", "error"}`**, not as an RFC 6749 §5.2 redirect
  with `error`/`error_description`. The consumer of that endpoint is our own consent page. A
  third-party client therefore learns nothing from a refused authorization request until the
  consent page performs that redirect — a known v1 limitation, pinned by
  `oauth/tests/test_error_response_contracts.py` so changing it stays a decision.
- **`org:tickets` grants real Stripe refunds and per-event revenue**, because `manage_tickets`
  gates them and v1 keeps the coarse scope with an honest label rather than splitting the
  permission key mid-flight. Splitting `manage_tickets` is the recommended follow-up. The two
  `manage_event` routes that reach the same money (cancel-with-refunds, refund preview) require
  `org:tickets` in addition, and `org:members`' label names the membership payment ledger,
  MRR and refunds that `manage_subscriptions` unlocks — the same honest-label rule.
- An MCP server built later must be served from the same origin as `OAUTH_ISSUER`: DOT's
  audience check is a prefix match on the request URI, so a different origin would need
  introspection or unrestricted tokens.

### Alternatives rejected

- **Authlib** — lacks RFC 8707 and RFC 9728, the two the MCP profile needs most, and has two
  contributors with 10+ commits against DOT's fourteen.
- **Hand-rolled authorization server** — ADR-0016 hand-rolled the *relying party* (one flow,
  one verifier) and that was the right call; a provider is the inverse problem, and the parts
  that are easy to get subtly wrong are exactly the parts a library has already been attacked
  over.
- **Organizer API keys** — no consent, no scopes, no per-app revocation; the degenerate case of
  this design is a developer authorizing their own app, which covers the same need.

### Upgrade paths (documented, not built)

Per-organization grant restriction at consent; an `org:attendees` scope (needs a per-route
allow-list); organization-installation actors and the client-credentials grant that depends on
them; per-client throttling keyed on `client_id`; introspection for a resource server on
another origin; RP-initiated logout and the device flow (DOT switches, no schema change); an
`OptionalScopedAuth` for personalized public reads; `org:financials` and `me:write` scopes.
