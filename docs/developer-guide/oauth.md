# Third-party apps (OAuth 2.1 / OpenID Connect)

Revel is an OAuth 2.1 authorization server and OpenID Provider. A third-party app, a script,
or an MCP host can call the Revel API **on a user's behalf** with a token limited to the scopes
that user approved. See [ADR-0018](../adr/0018-oauth-oidc-provider.md) for why it is built this
way.

The provider exists only when the instance has a signing key (`OIDC_SIGNING_KEY_PATH`) and an
issuer (`OAUTH_ISSUER`). Without them every route below returns `404`, and `GET /api/version`
reports `features.oauth_provider: false`.

Endpoints (issuer = the API origin, e.g. `https://api.letsrevel.io`):

| Purpose | URL |
|---|---|
| OIDC discovery | `/.well-known/openid-configuration` |
| AS metadata | `/.well-known/oauth-authorization-server` |
| Protected-resource metadata (RFC 9728) | `/.well-known/oauth-protected-resource` |
| Consent screen (frontend) | `{FRONTEND_BASE_URL}/oauth/authorize` |
| Token / revoke / userinfo / JWKS | `/o/token`, `/o/revoke`, `/o/userinfo`, `/o/jwks` |
| Dynamic registration (RFC 7591/7592) | `/o/register`, `/o/register/{client_id}` |

Token introspection, the device flow, client credentials and RP-initiated logout are not
mounted.

## Registering an app

In the web app: **Settings → Developer apps → New app**. Or over the API, with your own
session token (a verified email address is required):

```bash
curl -X POST https://api.letsrevel.io/api/oauth/apps/ \
  -H "Authorization: Bearer $SESSION_JWT" -H 'Content-Type: application/json' \
  -d '{"name": "My Tool", "description": "What it does",
       "client_type": "public",
       "redirect_uris": ["https://mytool.example/callback"],
       "allowed_scopes": ["openid", "profile", "org:read", "org:events"],
       "homepage_url": "https://mytool.example",
       "privacy_policy_url": "https://mytool.example/privacy"}'
```

- `client_type: "public"` — no secret, PKCE only (SPAs, CLIs, native apps).
- `client_type: "confidential"` — the response carries `client_secret` **once**; it is stored
  hashed and can only be rotated, never read back.
- `allowed_scopes` is the app's ceiling. Requesting a scope outside it fails with
  `invalid_scope`; shrinking it later immediately revokes live tokens that held the removed
  scope.
- A redirect URI must be `https`, or `http` on a loopback address (`127.0.0.1`, `[::1]`,
  `localhost`) for a public client. Fragments are rejected.
- Each user may own `OAUTH_MAX_APPS_PER_USER` apps (default 10); the next one is a `409`.

Revel staff can mark an app **verified**, which removes the "this app has not been reviewed"
warning from the consent screen. There is no way for an app to set that itself.

## Scopes

The table is the contract; the wording is what the user reads before approving.

| Scope | Consent label |
|---|---|
| `openid` | Sign you in |
| `profile` | See your name and picture |
| `email` | See your email address |
| `offline_access` | Stay connected |
| `me:read` | See your profile, tickets, RSVPs, memberships, invoices and payments |
| `org:read` | See your organizations, events and settings |
| `org:events` | Create, edit and delete events and event series, send invitations, and see attendee lists |
| `org:tickets` | Manage ticket tiers, tickets, discount codes and seating, issue refunds, and see attendee details and revenue |
| `org:checkin` | Check attendees in |
| `org:members` | Manage members, subscriptions and membership payments, including refunds, and see membership revenue |
| `org:announcements` | Send announcements |
| `org:questionnaires` | Manage and evaluate questionnaires |
| `org:polls` | Manage polls |
| `org:potluck` | Manage potluck items |

Six things to know:

- **There is no attendee *write* scope.** `me:read` is the only `me:` scope beyond the OIDC
  four, so RSVPing, buying a ticket, editing a guest name or starting a subscription cannot be
  done with an app token at all — those routes stay first-party only (see
  [What app tokens cannot reach](#what-app-tokens-cannot-reach)). An earlier draft advertised a
  `me:rsvp` scope that enforced nothing; it is gone rather than left on the consent screen
  promising a capability no app received.
- **`me:read` reaches your email address.** `GET /api/account/me` is a `me:read` route and its
  response carries `email`, `email_verified`, `totp_active` and `referral_code`. The OIDC
  `email` scope gates the `email` *claim* in the ID token and at `/o/userinfo` — it is not the
  only path to the address, so do not read `me:read` as "no contact details". It is the user's
  own profile, which is what "See your profile" says, but it is worth stating plainly.
- **`org:read` is a hard baseline on every organizer route.** An app that wants `org:events`
  must request `org:read` alongside it, or every organizer call fails with
  `insufficient_scope` naming `org:read`. It is the scope that lets an app onto the organizer
  surface at all.
- **`org:tickets` really does include money.** Beyond tiers and attendee lists it covers
  discount codes, seating, box-office sales, per-event revenue and **real Stripe refunds**,
  because they share one staff permission key. Request it only if you need it. It is also
  required *alongside* `org:events` for the two event-cancellation routes that touch money:
  `POST .../actions/update-status/cancelled` with `refund_tickets=true`, and
  `GET .../cancellation-refund-preview` (which reads the connected Stripe balance). Series
  passes are ticket products too: every route under `/api/event-series-admin/{id}/passes`
  (pricing, holders and what they paid, offline-payment confirmation, cancel-with-refund) needs
  `org:tickets` alongside the `org:events` their permission key maps to.
- **`org:members` includes membership money too.** `manage_subscriptions` gates the
  subscription plans, but also the organization's MRR/churn metrics, the membership payment
  ledger and recording or refunding a payment — which is why the label says so. It does
  **not** include organization invitation links (`/organization-admin/{slug}/tokens`): a link
  can make whoever claims it a staff member, and granting staff status is first-party only.
- **A scope is a ceiling, not a grant.** A token's power is *the scopes the user approved* ∩
  *what that user may do right now*. An app holding `org:tickets` gets `403` in an organization
  where the user lacks `manage_tickets`, and loses access the moment that permission is
  withdrawn. Some routes are unreachable with an app token whatever it holds — see
  [What app tokens cannot reach](#what-app-tokens-cannot-reach).
- **`me:read` includes your money, read-only.** Besides profile, tickets and memberships it
  returns your invoices (with a download link for each PDF) and your membership payment
  history — your own records only, never anyone else's.

## What app tokens cannot reach

Some routes accept only a **first-party token**: the JWT you get by logging in to Revel
yourself, the same one the web app uses. Code comments and reviews call these routes
*session-only*. "Session" here means *your own login*, not a browser cookie: this is the
classic way to use the Revel API, and everything below still works with it.

```bash
curl -X POST https://api.letsrevel.io/api/auth/token/pair \
  -H 'Content-Type: application/json' -d '{"username": "you@example.com", "password": "…"}'
# → {"access": "…", "refresh": "…"}
# With 2FA on, you get {"token": "…", "type": "otp"} instead: POST {"token": "…", "otp": "123456"}
# to /api/auth/token/pair/otp for the pair. Refresh with POST /api/auth/refresh.
```

That token is **you**, with everything your account can do. It is for your own scripts only.
Never ask someone else for their password to get one: acting on another user's behalf is what
app tokens are for, and the routes below are the ones a user cannot delegate.

**The rule.** A route accepts app tokens only if a scope's consent label honestly describes
what the route does. When no label does (you are acting as the organization *owner*, handling
money the scope never mentioned, changing your identity or security settings, or taking an
attendee action that has no write scope yet), the route stays first-party only. Accepting app
tokens is opt-in per controller, from a reviewed allow-list, and CI fails if a route on that
surface is owner-only, is a write gated only by a `:read` scope, or compares an owner in its
handler (`src/oauth/tests/test_scope_coverage.py`). Those guards read the controller layer
only. A rule enforced deeper, in a service, still needs a reviewer, which is how the
invitation links below slipped through.

**How a refusal looks.** A first-party-only route answers an app token with `401` and no
`insufficient_scope` challenge, so re-consenting will not help. Public routes that also accept
a login (event, organization, series and poll pages, guest checkout, seat holds) behave the
same way: they answer `401` to an app token, so call them **without** an `Authorization`
header to get the anonymous view. Routes with no authentication at all (city and tag lookups, the registration
and password-reset flows) ignore the header.

### Whole areas

Paths are relative to `/api`.

| Area | Routes | Why |
|---|---|---|
| Buying, RSVPing and joining | `/events/{id}/checkout*`, `/events/{id}/tickets/{tier}/checkout*`, `/events/{id}/rsvp/*`, `/events/{id}/waitlist/*`, `/events/{id}/seating/holds*`, `/events/{id}/invitation-requests`, `/events/{id}/questionnaire/*`, `/events/claim-invitation/*`, `/events/tickets/{id}/cancel`, `/series-passes/*`, `/organizations/{slug}/membership-requests`, `/organizations/claim-invitation/*`, following and unfollowing (`/organizations/{slug}/follow`, `/event-series/{id}/follow`) | No attendee write scope exists yet. Reading what you follow is `me:read` (`/me/following/*`) |
| Attendee views of an event | `/events/{id}/attendee-list`, `…/announcements`, `…/dietary-summary`, `…/pronoun-distribution`, `/organizations/{slug}/member-announcements` | Other attendees' data, shown to fellow attendees; no scope covers it |
| Public browsing | `/events/*`, `/organizations/*`, `/event-series/*`, `/polls/*` reads | Anonymous or first-party; app tokens get `401` (see above) |
| Account and security | `/account/*` except `GET /account/me`; `/otp/*`; `/telegram/*`; `/preferences/*`; `/dietary/*`; `/notifications*`; `/notification-preferences*`; `/exports/*`; `/permissions/my-permissions` | Your identity, security and personal settings are not delegable |
| Your billing and referrals | `/me/billing*`, `/me/referral/*`, `/referral/stripe/*` | Money and tax identity; no scope names them |
| Wallet passes and PDFs | `/tickets/{id}/pdf`, `/tickets/{id}/wallet/*`, `/me/organizations/{slug}/membership/{pdf,wallet/*}` | Not in any scope |
| Organization finances | `/organization-admin/{slug}/revenue*`; `…/vat-id`, `…/billing-info`, `…/invoicing`, `…/invoices*`, `…/credit-notes`, `…/attendee-invoices*`, `…/attendee-credit-notes` | Owner-only; waiting for a dedicated `org:financials` scope |
| Organization invitation links | `/organization-admin/{slug}/tokens*` (all methods) | A link can grant staff status (see below) |
| Creating organizations | `POST /organizations/`, `POST /organizations/{slug}/contact`, `POST /organizations/{slug}/whitelist-request` | Not delegable |
| Poll administration | `POST /polls/organizations/{org_id}`, and `PATCH`/`DELETE /polls/{id}/`, `…/open`, `…/close`, `…/reopen`, `…/duplicate`; voting (`/polls/{id}/vote`) | `org:polls` covers a poll's sections and questions (`PollQuestionController`), not the poll's lifecycle; voting is an attendee write |
| Platform integrations | `/organization-admin/{slug}/integrations*`, `/event-admin/{id}/integrations*` | Third-party credentials (e.g. Eventbrite); not in any scope |
| Questionnaire file uploads | `/questionnaire-files/*` | Not in any scope |
| The OAuth provider itself | `/oauth/apps/*`, `/oauth/authorize`, `/oauth/connections*` | An app must never manage apps or approve its own consent |

### Single routes inside app-token controllers

These controllers otherwise accept app tokens, which is why the exceptions are worth listing.
Paths are relative to `/api`. This table is exhaustive, and CI checks it against the code in
both directions (`src/oauth/tests/test_scope_docs.py`).

| Route | Why |
|---|---|
| `GET /organization-admin/{slug}` | Returns the organization's financial identity (Stripe, VAT), which `org:read` does not promise |
| `POST /organization-admin/{slug}/verify-contact-email` | Changes a verified identity |
| `POST /organization-admin/{slug}/stripe/connect`, `POST /organization-admin/{slug}/stripe/account/verify` | Owner-only Stripe onboarding |
| `POST /organization-admin/{slug}/staff/{user_id}`, `DELETE /organization-admin/{slug}/staff/{user_id}`, `PUT /organization-admin/{slug}/staff/{user_id}/permissions` | Owner-only; granting or changing staff powers |
| `PUT /account/me`, `PUT /account/language`, `POST /account/me/upload-profile-picture`, `DELETE /account/me/delete-profile-picture` | Account writes; `me:read` only reads your profile |
| `POST /account/email-change-request`, `GET /account/identities`, `DELETE /account/identities/{provider}`, `POST /account/delete-request`, `POST /account/export-data` | Identity, linked logins, deletion and data export are not delegable |
| `PATCH /dashboard/tickets/{ticket_id}/guest-name` | Attendee write |
| `POST /me/organizations/{slug}/apply`, `POST /me/applications/{application_id}/cancel` | Attendee write |
| `POST /me/organizations/{slug}/membership-questionnaire/{questionnaire_id}/submit` | Attendee write |
| `POST /me/organizations/{org_id}/subscribe`, `POST /me/organizations/{org_id}/subscription/cancel`, `POST /me/organizations/{org_id}/subscription/uncancel`, `POST /me/organizations/{org_id}/subscription/change-plan`, `POST /me/organizations/{org_id}/subscription/revive`, `POST /me/organizations/{org_id}/billing-portal` | Starting, changing or paying for a subscription; `me:read` only reads |
| `POST /events/{event_id}/potluck/`, `POST /events/{event_id}/potluck/{item_id}/claim`, `POST /events/{event_id}/potluck/{item_id}/unclaim` | Attendee write (`org:potluck` covers *managing* the list, not bringing a dish) |

!!! note "Why invitation links are first-party only"

    Claiming an organization invitation link that has `grants_staff_status` creates a staff
    member. `POST /staff/{user_id}` has always been first-party only, but the link routes used
    to accept `org:members`, so an owner-delegated app could mint a staff link (or list the
    existing ones) and pass it on. That reaches the same result by a different route. Member-only
    links are harmless in comparison, but one controller serves both kinds, so the whole
    controller is first-party only. Event invitation links (`/event-admin/{id}/tokens`) only
    invite attendees, and they stay on `org:events`.

## Authorization code + PKCE

PKCE is mandatory and there is no implicit and no password grant. `S256` is the only
supported method: `code_challenge_method=plain` is refused (RFC 9700 §2.1.1, RFC 7636 §4.2 —
it makes the challenge its own verifier, so a leaked code is directly exchangeable).
**Send `code_challenge_method=S256` explicitly.** RFC 7636 §4.3 defines the parameter's
default as `plain`, so omitting it is treated as `plain` and refused with
`error=invalid_request` — the same as sending it.

```bash
# 1. Verifier and challenge
VERIFIER=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
CHALLENGE=$(python3 - <<EOF
import base64, hashlib
print(base64.urlsafe_b64encode(hashlib.sha256("$VERIFIER".encode()).digest()).rstrip(b"=").decode())
EOF
)

# 2. Send the user to the consent screen (FRONTEND origin, not the API origin)
echo "https://letsrevel.io/oauth/authorize?client_id=$CLIENT_ID\
&response_type=code&redirect_uri=https://mytool.example/callback\
&scope=openid%20profile%20org:read%20org:events&state=$(uuidgen)\
&code_challenge=$CHALLENGE&code_challenge_method=S256\
&resource=https://api.letsrevel.io"

# 3. They approve; your callback receives ?code=…&state=…  (the code lives 60 seconds)
curl -X POST https://api.letsrevel.io/o/token \
  -d grant_type=authorization_code -d code="$CODE" \
  -d redirect_uri=https://mytool.example/callback \
  -d client_id="$CLIENT_ID" -d code_verifier="$VERIFIER"
  # confidential clients add: -d client_secret="$CLIENT_SECRET"

# 4. Call the API
curl https://api.letsrevel.io/api/dashboard/organizations \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

- `resource` (RFC 8707) binds the token to the API origin. Send it; without it a token is
  unrestricted, and a token bound to some other origin is refused with `invalid_token`.
- Access tokens live 1 hour. A refresh token is issued **only** when `offline_access` was
  granted; refresh tokens rotate on every use with reuse protection, so replaying an old one
  revokes the whole family (`invalid_grant`). That includes a refresh that narrows the scope
  (RFC 6749 §6): asking for fewer scopes — even leaving out `offline_access` — narrows the new
  access token but still rotates, so you always get a new refresh token back and must store it.
- `state` is yours: Revel echoes it and never inspects it.

## OIDC login

With `openid` you get an `id_token` (RS256, keys at `/o/jwks`) and can call
`GET /o/userinfo`. Claims are gated by scope: `sub` (`openid`); `name`, `picture`, `locale`
(`profile`); `email`, `email_verified` (`email`). Staff and superuser flags are never
emitted, there is no `preferred_username` (it would be the user's email address), and `name` is
absent when the user has filled in neither a preferred name nor a real name.

!!! warning "`picture` is a short-lived signed URL"

    The `picture` claim is an absolute, HMAC-signed URL that **expires after about an hour**,
    matching the ID token's lifetime. Do not persist it: cache it and your users get broken
    avatars an hour later. Re-fetch it from `/o/userinfo` whenever you need to display it.

Identify users by `sub` (a stable UUID). Email addresses change.

## Connecting an MCP host

Point the host at the API origin (`https://api.letsrevel.io`) and it does the rest:

1. It calls an API route without a token, gets `401` with
   `WWW-Authenticate: Bearer resource_metadata="…"` (no `error` attribute, per RFC 6750 §3.1;
   a rejected token gets `error="invalid_token"` as well), and follows that to
   `/.well-known/oauth-protected-resource` and then the AS metadata.
2. It registers itself at `/o/register` (RFC 7591) and receives a `client_id` plus a
   registration access token for RFC 7592 read/update/delete at `/o/register/{client_id}`.
   Dynamically registered clients have no owner and are never auto-`verified`; an
   unused one is pruned after `OAUTH_DCR_UNUSED_TTL_HOURS` (default 24), and registration is
   IP-throttled with a daily instance-wide cap (`Registration cap reached.` = `429`).
3. It opens the consent screen in a browser, the user approves, and it completes the PKCE
   exchange above.

Only a subset of the API accepts app tokens — organizer admin, dashboard, polls,
questionnaires and the attendee-side routes behind `me:` scopes. Anything else refuses an app
token by construction, whatever scopes it holds; the full list is in
[What app tokens cannot reach](#what-app-tokens-cannot-reach).

One asymmetry worth knowing: `GET /api/questionnaires/` needs only the `org:read` baseline,
while `GET /api/questionnaires/{id}` needs `org:questionnaires`. The list is a picker — names
and a pending-evaluation count, nothing a respondent wrote — whereas the detail route returns
the questionnaire's sections and questions and is the entry point to submissions and
evaluations. `org:read`'s label covers the first and not the second.

## Revoking access

- **Users**: *Settings → Connected apps* lists every app with live access, the scopes it holds,
  and when it was last used; removing one deletes its tokens immediately
  (`GET`/`DELETE /api/oauth/connections/{client_id}`).
- **Apps**: `POST /o/revoke` (RFC 7009) with an access or refresh token.
- **Developers**: deactivating your own app in the developer portal revokes every token issued
  for it, for all its users.
- Changing your email address disconnects every connected app, and a global ban revokes the
  banned user's tokens *and* deactivates the apps they own.

## Error responses

| Status | Body / header | Meaning |
|---|---|---|
| `400` | `{"detail", "error": "invalid_scope" \| "invalid_request" \| …}` | The authorization request was refused; the consent page renders it. |
| `400` | `{"detail", "error": "consent_required"}` | The consent screen expired — show it again. |
| `401` | `WWW-Authenticate: Bearer resource_metadata="…"` | No credentials were sent. Follow `resource_metadata` to discover the authorization server. |
| `401` | `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="…"` | Token unknown, expired, revoked, bound elsewhere, or its app or user was deactivated. Re-acquire it. |
| `403` | `WWW-Authenticate: Bearer error="insufficient_scope", scope="org:read"` | The user must re-consent with that scope. Without a `scope=` parameter, the route is unreachable by any app token. |
| `403` | `{"detail"}`, no challenge header | The scope is fine; the *user* lacks the organization permission. |
| `404` | `{"detail"}` | This instance has no OAuth provider configured. |
