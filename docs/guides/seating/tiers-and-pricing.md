# Tickets, Pricing & Seat Assignment

## What it is & who it's for

Every ticket a buyer holds is defined by a **ticket tier** — the thing that carries the
price, the sales rules, and (for seated venues) how a seat gets attached to it. Revel's
seating system layers three seat-assignment strategies on top of the tier model, so the
same venue map can power a 120-seat comedy club selling general admission and a
1,350-seat theatre selling numbered seats in three price bands — without different
software.

This page is for reps pitching organizers who sell **assigned or tiered seating**:
theatres, concert halls, conferences with reserved sections, comedy clubs with tables vs.
standing room. If the organizer only ever sells "one ticket, one price, sit anywhere,"
they'll barely notice this system exists — general admission (`NONE` mode) is the default
and needs zero extra configuration. The moment they want "front row costs more" or "let
people click their exact seat," this is the feature that does it.

Companion pages: how the venue map and seats are built (`venue-and-layout.md`), what the
buyer actually sees and clicks through (`buyer-experience.md`), and how event-day staff
handle seat overrides, door sales, and reseating (`box-office.md`).

## How it works

### The pricing model — the concept to nail

This is the single idea that makes the whole system click for a prospect:

> **The price category lives on the venue map. The price lives on the event.**

A **price category** (e.g. "Platea Premium", "Galleria", "Balcony") is a named, colored
label an organizer paints onto seats when they set up the venue — it lives on the
`Venue`, not the event. A **ticket tier** lives on one specific event and carries the
actual `price`. A tier points *at* a price category; it doesn't own one.

Splitting the two this way buys the organizer two things:

1. **Reusability.** The same venue layout (with the same painted categories) can be
   reused, unmodified, for every event held there — a Tuesday matinee and a Saturday gala
   can charge completely different prices for the identical Platea Premium seats, because
   the price lives on each event's tier, not on the seat.
2. **Concession pricing on one seat pool.** Because a category is just a label, **more
   than one tier is allowed to point at the same price category**. An organizer can sell
   "Platea Adult" at €45 and "Platea Student" at €30, both drawing from the exact same
   block of physical seats. The buyer picks whichever tier fits them; the seat pool is
   shared, not duplicated per price point. This is a genuinely strong selling point for
   theatres and concert halls running family/concession pricing — it's not a workaround,
   it's how the model is designed to work.

Concretely, in the seeded showcase venue **Teatro Grande** (~1,350 seats: Platea,
Galleria, four private Palchi boxes), the Galleria has one price category ("Galleria",
painted purple) and one best-available tier per event drawing from it. Nothing stops an
organizer from adding a second, cheaper "Galleria Restricted View" tier against that same
category for a different event.

### The three seat-assignment modes

Every ticket tier has a `seat_assignment_mode`. The backend validates each mode's
requirements on save — get the setup wrong and the tier is rejected before it can ever go
on sale:

| Mode | What the buyer experiences | Server requirement |
|---|---|---|
| **`none`** | General admission — no assigned seat. Optionally tied to a standing sector with a hard head-count. | Nothing required. A sector is optional; if given, it must not be a seated one used for `user_choice`. |
| **`user_choice`** | Buyer sees the chart and clicks their own seat(s). | **Requires a seated sector.** Pointing this mode at a standing sector is rejected outright — a standing sector has no individual seats to choose from. |
| **`best_available`** | Buyer requests a quantity; the system finds and holds the best block automatically. | **Requires a price category.** That category's painted seats are the pool the algorithm draws from. |

A few knock-on rules worth knowing when demoing:

- `none` tiers can optionally link to a **standing sector** — think "Standing Room" at a
  comedy club or the floor at a music venue. That sector's `capacity` becomes a hard cap
  enforced at the moment of sale: sell past it and the buyer gets a clear "this sector is
  full" rejection, never an oversell.
- A tier's venue, sector, and price category must all agree — they must belong to the
  same venue and organization, and if the event itself has a venue set, the tier's venue
  must match it. Organizers can't accidentally point a tier at a category from a
  different building.
- The Chuckle Cellar (the seeded comedy-club example) shows the contrast well: table
  seats sold `user_choice` (pick your table), riser seats also `user_choice`, and a
  "Standing Room" sector sold plain `none` with a 40-person hard cap. Same venue, three
  different commercial experiences.

### How best-available picks seats

When a `best_available` tier is bought — either after a hold or, as a fallback, right at
purchase time — the system doesn't just grab the first free seats. It scores every
possible adjacent block in the category's pool and keeps the lowest-scoring one:

1. **Front rows win.** Row order is the dominant factor — a front-row block always beats
   a back-row block, full stop.
2. **Centered in the row.** Among same-row options, it prefers the block closest to the
   row's true midpoint (calculated from the whole physical row, not just what's still
   available — so the "center" doesn't drift as seats sell).
3. **Keeps the party together.** It only considers contiguous runs of seats — a party of
   4 gets 4 adjacent seats, never split across a gap.
4. **Avoids stranding a single seat.** Among equally-central options in the same row, it
   avoids leaving a lone unsellable seat on either side of the block — good for keeping a
   near-sold-out row still sellable late in the on-sale.
5. Only placements that are *genuinely* tied on all of the above are then broken by a
   seeded random shuffle — so repeat requests aren't robotically deterministic, but a
   worse row or a stranded-seat option is never chosen just by chance.

If a party's requested quantity doesn't fit anywhere as one contiguous block, the answer
is a clean "not enough adjacent seats" outcome — no partial or split assignment is ever
silently made.

**Accessible seats are protected.** Wheelchair-accessible seats are excluded from the
general best-available pool entirely — an ordinary request for 2 seats will never
consume them, even if they'd otherwise score well. They're only drawn from when a buyer
explicitly asks for accessible seating (`accessible_required`), at which point the system
switches to the accessible-only pool and relaxes the adjacency rule to "nearest available
row" rather than "must be contiguous." The buyer-facing flow for that toggle — including
how it survives guest checkout across devices — is covered in `buyer-experience.md`.

### Tier mechanics that still apply

Seat assignment is layered on top of the ticket tier, not a replacement for it — every
other tier feature organizers already rely on keeps working unchanged:

- **Quantity caps** (`total_quantity`) and **per-user limits** (`max_tickets_per_user`,
  overridable per tier) still gate how many tickets of a seated tier can be sold.
- **Sales windows** (`sales_start_at` / `sales_end_at`) still gate *when* a tier is
  purchasable, seated or not.
- **Membership restrictions** — a tier can still be members-only, or restricted to
  specific membership tiers — regardless of whether it also carries a seat assignment
  mode.
- **Pay-what-you-can (PWYC)** pricing with a min/max range works the same on a seated
  tier as on a GA one — the seat is assigned or chosen independently of what the buyer
  actually pays.
- **Visibility and purchasability** rules (public / members-only / invited, and the
  invitation-linked-tier restrictions) apply identically.

In short: seating decides *which physical seat* a ticket occupies. Everything about
*whether this buyer is even allowed to buy this tier* is unchanged.

## Demo it

A clean live-demo path using the seeded showcase venues (bootstrapped with
`make bootstrap` / `make seed` in a dev environment):

1. **Show the pricing split first.** Open the organizer admin for the org that owns
   **Teatro Grande**, go to venue price categories
   (`/organization-admin/{org_slug}/venues/{venue_id}/price-categories`), and show the
   four painted categories: Platea Premium, Platea, Galleria, Palco — each just a name +
   color, no price in sight. That's the hook: "the price isn't here."
2. **Open a ticket tier for one of that venue's events** (e.g. "La Traviata — Season
   Opening") in the event admin ticket-tiers screen. Show the Galleria tier: mode
   `best_available`, price category = Galleria, price = €25 on *this* event. Point out
   that the same Galleria category, on the New Year's Gala event, could carry a
   completely different price.
3. **Contrast the modes on one venue.** Still on Teatro Grande: the Platea tier is
   `user_choice` (a seated sector, buyer picks their own seat); the Palco 1 tier is also
   `user_choice` against a private box sector; Galleria is `best_available`. Same venue,
   three tiers, three different buying experiences.
4. **Show the buyer-side chart and availability** for that event:
   `GET /events/{event_id}/seating/chart` (the render-ready map: sectors, seats, painted
   categories) and `GET /events/{event_id}/seating/availability` (sparse — only sold /
   blocked / held seats are listed, everything else is free).
5. **Trigger a best-available hold** against the Galleria tier:
   `POST /events/{event_id}/seating/holds/best-available` with `tier_id`, `quantity`
   (try 4 to show adjacency), and optionally `accessible_required: true` on a second call
   to show the accessible pool switch. Compare the two seat blocks returned.
6. **Show the "sold out block" case** by requesting a quantity larger than any remaining
   contiguous run — the response is a clean 409 rather than a scattered assignment.
7. **For the comedy-club contrast**, switch to **The Chuckle Cellar**: front tables and
   riser sold `user_choice`, standing room sold `none` with a 40-person hard cap. Good
   for prospects who think "seating" only means theatres.

## Talking points & FAQs

**"Can we charge students less for the same seats as everyone else?"**
Yes — that's exactly what shared price categories are for. Create a second tier (e.g.
"Platea Student") pointing at the same Platea category as the adult tier, at a lower
price. Both tiers draw from the identical seat pool; there's no seat duplication and no
separate map to maintain.

**"What happens if two people try to buy the last two adjacent seats at once?"**
Best-available holds are acquired optimistically and re-checked at lock time — if a seat
picked in the first pass gets taken by someone else in the same instant, the system
retries automatically excluding it. If nothing of the requested size is left afterwards,
the buyer gets a clear "not enough seats" response, never an overbook.

**"Will the system ever put a wheelchair user's ticket in a random seat, or give an
able-bodied buyer an accessible seat by mistake?"**
No on both counts. Accessible seats sit in their own protected pool — ordinary
best-available requests can't touch them. A buyer only gets one when they explicitly ask
for accessible seating, and that request always comes from the accessible-only pool.

**"Can we sell general admission and reserved seating at the same event?"**
Yes. Seat assignment mode is set per tier, not per event — a `none` GA tier and a
`user_choice` or `best_available` seated tier can coexist on the same event, even pointed
at different sectors of the same venue (e.g. GA floor + reserved balcony).

**"Does seating support pay-what-you-can, membership pricing, per-user limits?"**
Yes — those are existing tier features and none of them are seat-assignment-specific.
A seated tier can be PWYC, members-only, capped at N per buyer, or all three, exactly
like a GA tier.

**Honest limits:**
- `best_available` cannot split a party across rows — if the party doesn't fit in one
  contiguous run anywhere in the category's pool, it's a hard no, not a partial or
  best-effort placement.
- `user_choice` cannot be pointed at a standing sector — there's nothing to click on.
  Standing sectors are `none`-mode only, gated by a head-count cap.
- Best-available assignment (both hold and purchase) has no manual "prefer this exact
  row" override for the buyer — it optimizes automatically. Manual seat control at that
  level of precision is a box-office (staff) action, not a buyer self-service one — see
  `box-office.md`.

## Under the hood

For the technically-curious rep or a prospect's technical evaluator:

- **Models**: `TicketTier` (`src/events/models/ticket.py`) carries `price`,
  `seat_assignment_mode` (`SeatAssignmentMode.NONE` / `USER_CHOICE` / `BEST_AVAILABLE`),
  and optional FKs to `venue`, `sector`, and `price_category`. `PriceCategory`
  (`src/events/models/venue.py`) is venue-scoped (`name`, `color`, `display_order`) —
  its own docstring states the split plainly: *"Category lives on the map; price lives on
  the event via TicketTier. Multiple tiers MAY reference the same category (shared seat
  pool)."*
- **Validation**: `TicketTier._validate_venue_sector` and
  `_validate_seat_assignment_mode` (same file) enforce the per-mode requirements
  server-side at `clean()` time — `BEST_AVAILABLE` without a `price_category_id`, or
  `USER_CHOICE` without a `sector_id` (or pointed at a standing sector), are rejected
  before save. This is the single source of truth for the rule; the create/update API
  schemas mirror the same checks for a fast 422 without a round-trip.
- **Best-available scoring**: the pure scoring function lives in
  `src/events/service/seating/best_available.py` (`pick_best_available` /
  `_pick_general` / `_pick_accessible`) — row order, centrality, fragmentation penalty,
  sector order, in that priority, with a seeded shuffle only across genuinely-tied
  placements. Accessible seats are filtered out of `_pick_general`'s input entirely,
  which is what makes the protection unconditional rather than a fallback.
- **Candidate loading**: `src/events/service/seating/pick.py` (`load_candidates`,
  `hold_best_available`) is the DB-facing half — it excludes sold/held/overridden/
  inactive seats, computes each row's real physical bounds (so centrality scores against
  the true midpoint, not the shrinking available pool), and retries the pick up to 3
  times if a chosen seat is taken between the unlocked read and the lock.
- **Key endpoints**: `GET /events/{event_id}/seating/chart`,
  `GET /events/{event_id}/seating/availability`,
  `POST /events/{event_id}/seating/holds`,
  `POST /events/{event_id}/seating/holds/best-available`,
  `DELETE /events/{event_id}/seating/holds` (public/buyer side, all in
  `src/events/controllers/event_public/seating.py`); tier CRUD at
  `GET/POST /event-admin/{event_id}/ticket-tier(s)`,
  `PUT/DELETE /event-admin/{event_id}/ticket-tier/{tier_id}` (organizer side, in
  `src/events/controllers/event_admin/tickets.py`).
- **Full journey narrative**: see [`USER_JOURNEYS.md` §19, "Venue &
  Seating"](../../../USER_JOURNEYS.md#journey-19-venue--seating), particularly §19.3
  (tier seating configuration) and §19.5 (best-available buying flow).
