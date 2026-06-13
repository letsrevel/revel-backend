# PM-0003: SSR POST Bodies Silently Dropped on the Internal API Path (Login Outage)

## Summary

For roughly two days, nobody could log in or register through the website.
The SvelteKit server's new internal API path (`handleFetch` rewriting SSR
calls to `http://web:8000`, frontend #393/#394 + infra #5) re-wrapped each
outgoing request with `new Request(url, request)`. That construction turns the
body into a stream, which Node's `fetch` (undici) transmits with
`Transfer-Encoding: chunked` and **no `Content-Length`**. Django's WSGI layer
reads exactly `CONTENT_LENGTH or 0` bytes, so every POST body arrived empty
and Pydantic answered `422 {"msg": "Field required"}`. Users saw a red
"Field required" banner on a fully filled login form. No alert fired at any
layer; the outage was discovered by a user report (the operator himself
failing to log in).

## Detection

Human report with a screenshot, ~2 days after the regression shipped. No
monitoring signal existed for this failure mode: the backend logged the 422s
as `INFO request_finished`, the frontend login action converted the error
into a polite `fail(400)` without logging, and the frontend container is not
included in log collection at all.

## Timeline

All times CEST.

- **2026-06-10 20:46–22:33** — Internal SSR→API path ships: frontend #393
  (handleFetch rewrite + real visitor IP), infra #5 (`INTERNAL_API_URL=http://web:8000`).
  The first deploy attempt breaks all SSR pages via `SECURE_SSL_REDIRECT`
  (fixed by #394 sending `X-Forwarded-Proto`); the body-loss bug ships
  alongside and remains, invisible because GETs have no body.
- **2026-06-10 → 06-12** — Zero successful logins on `POST /api/auth/token/pair`
  (Loki, 72h window: 19×422 from `user_agent=node`, no 200s after the path
  went live). Registration 422s likewise. SSR pages keep rendering, so the
  site looks healthy.
- **2026-06-12 ~20:16** — Operator fails to log in, screenshot taken,
  investigation starts.
- **2026-06-12 evening** — Root cause confirmed by experiment; fix
  (frontend #398, v1.59.3) merged: buffer the body in `handleFetch`.

## Root Cause

`src/hooks.server.ts` (frontend) rewrote SSR API calls with:

```ts
const rewritten = new Request(internalApiUrl + path, request);
```

Constructing a `Request` **from another `Request`** discards the body's known
length — undici then sends the body as a stream with
`Transfer-Encoding: chunked` and no `Content-Length`. (A single wrap from a
plain init object, as the generated API client does, preserves
`Content-Length`; only the double wrap loses it. Both behaviors were verified
against a raw echo server.)

Django reads request bodies via WSGI semantics: `CONTENT_LENGTH or 0` bytes.
A chunked request therefore parses as an **empty body**, and Django Ninja's
Pydantic layer rejects it with `422 Field required` — exactly the error users
saw, since the login form's values never reached the backend.

Three masking layers explain why it shipped and stayed invisible:

1. **The public path buffers.** Caddy/Cloudflare absorb the request and
   re-emit it with `Content-Length`, so the same code worked everywhere
   except direct-to-gunicorn. The bug is not exotic: it reproduces against
   plain `runserver` too.
2. **Dev/CI never executes the code.** `handleFetch` only rewrites when
   `INTERNAL_API_URL` is set — production-only. Playwright e2e (login
   included) runs against the dev server with the variable unset, so the
   buggy line had zero coverage by construction.
3. **GETs are unaffected.** SSR page rendering kept working, so the
   regression surfaced only on credentialed POSTs (login, register, server
   actions).

## Impact

- **Login and registration fully broken** for all users for ~2 days (any SSR
  form action POSTing JSON to the API). Already-authenticated sessions kept
  working (token refresh in `hooks.server.ts` uses module-scope fetch, which
  bypasses `handleFetch` and goes out the public path).
- No data loss or corruption; the backend correctly rejected the empty
  requests.
- Reputational cost unquantified (users silently bounced by an error that
  blamed their input).

## Resolution

1. **Fix (frontend #398, v1.59.3):** `handleFetch` buffers the body
   (`await request.arrayBuffer()`) and rebuilds the rewritten request from
   `{method, headers, body}`, so undici sends a proper `Content-Length`.
   GET/HEAD keep an undefined body. Verified end-to-end: rewrapped login
   POST 422 → buffered rewrap 200 with token pair.
2. **Codebase sweep:** confirmed the double-wrap pattern exists nowhere else
   in the frontend; the generated client's single wrap is safe; no custom
   request interceptors are registered. `handleFetch` is the single
   chokepoint for SSR API traffic, so the fix is structurally containing.
3. **Rollback switch documented:** unsetting `INTERNAL_API_URL` restores the
   public path immediately if the internal path ever misbehaves again.

## Lessons Learned

- **An intermediary that "just works" can be load-bearing.** The buffering
  proxy was silently normalizing requests; removing it from the path changed
  request semantics (chunked vs. measured) in a way no layer asserted.
- **Re-wrapping `Request` objects is a footgun.** `new Request(url, request)`
  looks like the canonical pattern (it appears in SvelteKit docs) but changes
  body framing under undici. When forwarding bodies across trust/transport
  boundaries, buffer explicitly.
- **"No errors" is not "no outage."** Every layer behaved "correctly" by its
  own contract and logged at INFO or below. Detection requires signals tied
  to user outcomes, not error levels: a synthetic login canary and an
  absence-of-successful-logins alert (tracked in revel-frontend #400, with
  full frontend observability parity: structured logs → Loki, metrics,
  alert rules).
- **CI must test the deployment shape, not just the code.** A
  config-gated code path (`INTERNAL_API_URL`) had zero test coverage by
  construction. Follow-up: a prod-shaped smoke job running the real frontend
  image with the variable set against a local backend (login/register specs
  only).
- **422s on schema-validated forms are a contract-violation signal.** The
  frontend validates with Zod before sending; a backend 422 therefore always
  indicates a bug, never user error. Worth logging at error level
  frontend-side.

## References

- Fix: [revel-frontend #398](https://github.com/letsrevel/revel-frontend/pull/398) (v1.59.3)
- Observability/alerting follow-up: [revel-frontend #400](https://github.com/letsrevel/revel-frontend/issues/400)
- Introduced by: [revel-frontend #393](https://github.com/letsrevel/revel-frontend/pull/393),
  [#394](https://github.com/letsrevel/revel-frontend/pull/394), infra #5
- Background: WSGI reads `CONTENT_LENGTH or 0` — chunked requests parse as
  empty bodies (PEP 3333 input semantics).
