# ADR-0017: Platform listings via a provider protocol

## Status

Accepted

## Context

Organizers wanted the same event listed on Revel and on other ticketing platforms without doing
it by hand. Eventbrite is the first platform; the design needed to generalize to a second one
without a rewrite, while keeping Revel's own capacity accounting, permissions, and attendee data
untouched by whatever a third-party API does or returns.

Options considered were a bespoke Eventbrite integration versus a generic abstraction from day
one. A half-day spike against a throwaway Eventbrite account (2026-09-04) closed the open
questions — auth flow, rate limits, webhook shape, hidden-ticket-class semantics — before any
phase landed.

## Decision

A new `integrations` app owns a `t.Protocol`-based `ListingProvider` plus neutral pydantic shapes
(`RemoteEvent`, `RemoteTicketClass`, `TokenSet`, …); provider-specific JSON, HTTP quirks, and
translation logic are confined to `providers/<name>/` (`providers/eventbrite/`), so a second
platform adds a client and a translator and nothing else. `integrations` imports from `events`;
`events` never imports from `integrations` (auto-sync hooks in through signals, the pattern the
`telegram` app already uses).

Policy decisions baked into the protocol and the sync/reconcile services:

- **Revel is the source of truth after linking.** Manual push by default; a push always sends
  the full mapped state, and there is no pull-back of remote edits.
- **Counts only cross platforms.** Revel reads sold quantity per remote ticket class and nothing
  else — no attendee data ever enters or leaves Revel through an integration.
- **First push creates a draft; publish is a separate, explicit action** — in Revel, or the
  organizer publishing on the platform directly, after which pushes mirror Revel's status.
- **Auto-sync is opt-in and only maintains already-pushed listings** — enabled per connection or
  overridden per event, it never pushes a new event on its own.
- **Reconciliation never touches remote-only ticket classes.** Unmappable tiers get no
  `TierLink`; a remote class Revel didn't create is left alone.
- **`TierLink.remote_paused` is written only by the pause/resume flow** — it is what Revel last
  set, and the mapper reads it on every push so a paused tier doesn't un-hide itself.
- **Webhooks are unsigned pointers**, resolved by rebuilding the resource URL from the
  provider's own base host plus the parsed path (never the payload URL as-is) and fetched with
  the organization's own token; numeric-only ids in that path close off percent-encoding tricks.
- **The app-key rate budget is shared instance-wide**, not per organization; the 15-minute
  reconcile processes links oldest-`counts_updated_at` first and stops once the remaining budget
  drops below a configurable reserve (`INTEGRATIONS_RATE_RESERVE`), so organizer-initiated push
  and pause calls never starve behind the sweep.
- **`listed` follows Revel's own visibility** — a private event is never mirrored regardless of
  connection or auto-sync state.
- **The `provider` column is unconstrained** (no `choices=`); the registry is the single point
  that validates a provider key, both for real providers and the in-memory fake used by every
  non-translator test.

## Consequences

- A second platform is a client + translator behind the same protocol; no changes to the sync
  service, signals, controllers, or error-code contract.
- One schema migration per app per PR, squashed before merge into `main`
  (`integrations/migrations/0001_initial.py` + `0002_beat_tasks.py`, the latter a data migration
  seeding the beat rows).
- Eventbrite-specific gotchas the generic design has to route around, all confirmed in the spike:
  - Eventbrite's CloudFront WAF returns 403 on `/oauth/authorize` whenever `redirect_uri` points
    at a loopback host (`localhost`, `127.0.0.1`, any scheme) — local OAuth testing needs a
    non-loopback alias for the API.
  - The listing summary is capped at exactly 140 characters, distinct from the full description.
  - Structured content (the description) and the plain-text summary can conflict, so the mapper
    derives a summary from the description's first text-bearing block rather than trusting a
    single field.
  - Venues are reused by id rather than recreated on every push.
  - The rate limit is 2000 calls/hour **per token and per app key simultaneously**; every
    organization's OAuth token shares the same app-key bucket, which is why the budget is
    instance-wide rather than per connection.
- The create path has a narrow orphan window: a worker dying between `create_event` and the
  immediate `link.save` that records the returned `remote_id` leaves one orphaned draft on the
  platform (harmless — a draft is not public), and the retry creates a second listing it then
  keeps updating; every attempt after that takes the update path.
- Attendee import, two-way sync, shared-capacity arithmetic, and any provider beyond Eventbrite
  remain explicitly out of scope for this phase.
