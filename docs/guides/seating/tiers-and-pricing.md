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

> **Paint zones on the venue. Price zones on the tier. Give a zone its own tier only
> when it needs its own cap or sales window.**

A **price category** — a *zone* — (e.g. "Platea Premium", "Galleria", "Balcony") is a
named, colored label an organizer paints onto seats when they set up the venue: it lives
on the `Venue`, not the event. A **ticket tier** lives on one specific event and carries
the actual money, as a **price per zone**. A tier points *at* the painted zones; it never
owns them.

There is exactly **one** way a tier turns painted zones into money — the tier's zone price
map — and it works the same whether the buyer picks their own seat or the system picks it
for them. There is no second, parallel pricing mechanism to choose between.

Splitting the two this way buys the organizer two things:

1. **Reusability.** The same venue layout (with the same painted categories) can be
   reused, unmodified, for every event held there — a Tuesday matinee and a Saturday gala
   can charge completely different prices for the identical Platea Premium seats, because
   the price lives on each event's tier, not on the seat.
2. **Concession pricing on one seat pool.** Because a zone is just a label, **more
   than one tier is allowed to price the same zone**. An organizer can sell
   "Platea Adult" at €45 and "Platea Student" at €30, both drawing from the exact same
   block of physical seats. The buyer picks whichever tier fits them; the seat pool is
   shared, not duplicated per price point. This is a genuinely strong selling point for
   theatres and concert halls running family/concession pricing — it's not a workaround,
   it's how the model is designed to work.

Concretely, in the seeded showcase venue **Teatro Grande** (~1,350 seats: Platea,
Galleria, four private Palchi boxes), the Galleria has one price zone ("Galleria",
painted purple) and one best-available tier per event pricing it. Nothing stops an
organizer from adding a second, cheaper "Galleria Restricted View" tier that prices the
same zone for a different event.

### The zone price map — the one mechanism

A seated tier — in **either** seat-assignment mode — carries a **zone price map**: a price
per painted category, set on the tier, alongside its flat `price`. Concretely, for one
night at Teatro Grande:

| Painted category | Price on this tier |
|---|---|
| Platea Premium | €80.00 |
| Platea | €50.00 |
| Galleria | €30.00 |
| *unpainted seats* | the tier's flat `price` |

The buyer opens the map, picks seat A-7 (Platea Premium) and seat M-22 (Platea), and pays
**€130** — one cart, one checkout, one order. Every downstream number follows: each ticket
records what *it* cost, a discount code applies per ticket (10% off that cart is €8 + €5,
not 2 × the same figure), the Stripe receipt shows two different line amounts, and the
revenue report reads back exactly what was collected.

Four rules to have at your fingertips when you demo this:

1. **An empty map is flat pricing, and it's normal.** A seated tier with no zone prices
   sells its whole sector at the tier's flat `price` — in either mode. That's the
   comedy-club case ("every seat in this room is €20"), and it needs no setup at all.
2. **A non-empty map means different things in the two modes** — this is the one
   distinction worth memorising:
   - **`user_choice` → every painted category *should* be priced.** The buyer can click
     any seat, so a painted category the map omits is a seat that cannot be sold. This is
     **advisory, not a refusal**: the tier saves, the admin screen names the unpriced
     categories (`pricing_gaps`), and checkout refuses exactly those seats. It cannot be a
     save-time rule, because painting is venue-wide and always succeeds — a rule against
     it would never prevent the gap, only block the organizer's next unrelated edit (and,
     worse, block duplicating the event or generating the next date of a series).
   - **`best_available` → the map's keys *are* the tier's sellable zones.** A partial map
     is not a mistake, it's the feature: a tier that prices only "Platea Premium" sells
     *only* Platea Premium, and the rest of the sector is simply not part of this product.
     A painted category the map omits is **not** reported as a gap and never will be —
     that would be a permanent false alarm on a deliberately-scoped tier. The *converse*
     is worth watching, and is reported: a zone the tier prices that no live seat in its
     sector carries can never yield a seat, so the admin screen lists it
     (`unsellable_zones`) — typically a typo, or a category from the wrong sector.
3. **Unpainted seats fall back to the flat price.** That's the one legitimate fallback,
   and it's deliberate: a sector where only the front rows are painted works fine.
4. **Zone pricing and pay-what-you-can are mutually exclusive.** A tier is one or the
   other. PWYC means the buyer names the price, which is the opposite of the seat naming
   it.

> **The one gap to be honest about (user-choice).** Painting is venue-wide and always
> succeeds — it must, or one event's pricing setup would block routine map work for every
> other event in that building. So an organizer *can* paint a new category onto seats after
> a `user_choice` tier was saved, and that tier now has a category it doesn't price. Revel
> does **not** quietly sell those seats at the flat price (an €80 seat for €50 is exactly
> the mistake this feature exists to prevent). Instead:
>
> - **Only the affected seats stop selling.** Everything in a priced zone keeps selling
>   normally.
> - Buyers see those seats greyed out on the map, and checkout returns a clear message
>   naming the zone if they get that far. The box office gets the same refusal on a door
>   sale — a wrong price at the door is as bad as a wrong price on the web.
> - The tier's admin screen flags the gap (`pricing_gaps`), so the organizer sees it before a
>   buyer does.
> - **The fix takes seconds**: add a price for that zone and every future sale is right.
>
> On a `best_available` tier there is no equivalent gap — an unpriced painted category is
> simply outside that tier's zones, so nothing to fix and nothing reported. Its own hazard
> is the mirror image: a zone the tier prices that nothing in the sector carries (someone
> unpainted the last of those seats, or picked a category from the wrong sector). Buyers
> can select that zone and the picker will never find a seat for it, so the tier's admin
> screen lists it as an **unsellable zone**. Same fix, same seconds: repaint the seats, or
> drop the zone from the map.

**Repainting can move money on a best-available tier too.** Because both modes read the
paint, a repaint that moves seats between two zones a tier prices changes what those seats
cost — silently, at every event in the building. The paint screen's advisory therefore
reports **both** seated modes: it names each live tier whose prices this paint would move,
before you commit it. See [`venue-and-layout.md`](venue-and-layout.md).

**Duplicate and repeat freely.** Duplicating a seated event — and therefore generating
every occurrence of a recurring one — carries the venue, the sector and the zone price map
across with the rest of the tier. A theatre configures opening night and gets the other
nineteen dates fully priced, without touching a tier again. (If a prospect tried this on
an older build and hit an error duplicating a seated event: that's fixed.)

### The buyer picks the zone — best-available

On a `user_choice` tier the buyer picks a *seat*, and the seat's paint decides the price.
On a `best_available` tier the buyer never sees a seat before choosing — so they pick the
**zone** instead, and the system finds the best block inside it:

> "2 tickets, **Galleria**, best available" — the zone is part of the request, exactly as
> the quantity is.

The rules are short and strict, because a mis-selected zone is a money bug:

- **On a mapped tier the zone is required.** Missing, unknown, or naming a category that
  isn't one of *this tier's* zones all give a **400 that lists the tier's zones**. This
  holds even when the tier has exactly **one** zone — the client must send it explicitly.
  There is no "we'll guess since there's only one."
- **On an unmapped (flat) tier, or any non-best-available tier, sending a zone is also a
  400.** A parameter the buyer believes selected a zone, quietly ignored, is worse than an
  error.
- **The pool is the tier's sector ∩ the chosen zone.** Painting "Balcony" in two different
  sectors can no longer produce a cross-sector block: a tier draws only from its own sector.
- **What you held is what you buy.** If the seats were held under one zone and checkout
  names a different one, that's a **409** — never a silent substitution onto other seats.

### Per-zone capacity — give the zone its own tier

"Sell at most 50 Premium and 200 Standard" is a real requirement, and the shape of the
answer is one tier per capped zone:

| Tier | Mode | Zone map | `total_quantity` |
|---|---|---|---|
| Premium — Best Available | `best_available` | `{Platea Premium: €80}` | 50 |
| Standard — Best Available | `best_available` | `{Platea: €45}` | 200 |

Both tiers point at the same sector; each sells only its own zone and each carries its own
cap, its own sales window, and its own per-user limit. The same trick gives a zone an early
on-sale, a members-only restriction, or a different refund policy.

**There is no per-zone counter *inside* one tier** — a single tier's `total_quantity` spans
all of its zones, deliberately. If a zone needs its own number, it needs its own tier. Say
that plainly rather than implying a limit that isn't configurable.

### The three seat-assignment modes

Every ticket tier has a `seat_assignment_mode`. The backend validates each mode's
requirements on save — get the setup wrong and the tier is rejected before it can ever go
on sale:

| Mode | What the buyer experiences | Server requirement |
|---|---|---|
| **`none`** | General admission — no assigned seat. Optionally tied to a standing sector with a hard head-count. | Nothing required. A sector is optional; if given, it must not be a seated one used for `user_choice`. |
| **`user_choice`** | Buyer sees the chart and clicks their own seat(s) — at one flat price, or at a price per painted zone. | **Requires a seated sector.** Pointing this mode at a standing sector is rejected outright — a standing sector has no individual seats to choose from. A zone price map is optional; partial coverage saves, and the unpriced categories are flagged on the tier (and refused at checkout). |
| **`best_available`** | Buyer requests a quantity (and, on a mapped tier, a zone); the system finds and holds the best block automatically. | **Requires a seated sector** too — the sector bounds the pool. A zone price map is optional; when present, its keys are the tier's sellable zones and the buyer must name one. |

A few knock-on rules worth knowing when demoing:

- `none` tiers can optionally link to a **standing sector** — think "Standing Room" at a
  comedy club or the floor at a music venue. That sector's `capacity` becomes a hard cap
  enforced at the moment of sale: sell past it and the buyer gets a clear "this sector is
  full" rejection, never an oversell.
- A tier's venue, sector, and priced zones must all agree — they must belong to the
  same venue and organization, and if the event itself has a venue set, the tier's venue
  must match it. Organizers can't accidentally price a zone from a different building.
- The Chuckle Cellar (the seeded comedy-club example) shows the contrast well: table
  seats sold `user_choice` (pick your table), riser seats sold `best_available`, and a
  "Standing Room" sector sold plain `none` with a 40-person hard cap. Same venue, three
  different commercial experiences.

### How best-available picks seats

When a `best_available` tier is bought — either after a hold or, as a fallback, right at
purchase time — the system doesn't just grab the first free seats. The pool is the tier's
**sector**, narrowed to the **zone the buyer asked for** when the tier is mapped. Inside
that pool it scores every possible adjacent block and keeps the lowest-scoring one:

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
  overridable per tier) still gate how many tickets of a seated tier can be sold. The cap
  is per *tier*, spanning all of its zones — see
  [per-zone capacity](#per-zone-capacity-give-the-zone-its-own-tier) for the one-tier-per-
  capped-zone pattern.
- **Sales windows** (`sales_start_at` / `sales_end_at`) still gate *when* a tier is
  purchasable, seated or not.
- **Membership restrictions** — a tier can still be members-only, or restricted to
  specific membership tiers — regardless of whether it also carries a seat assignment
  mode.
- **Pay-what-you-can (PWYC)** pricing with a min/max range works the same on a seated
  tier as on a GA one — the seat is assigned or chosen independently of what the buyer
  actually pays. The one exclusion: a tier can be PWYC **or** zone-priced, never both
  (the buyer names the price or the seat does).
- **Discount codes** apply per ticket, so a percentage code on a mixed-price cart takes
  the right amount off each seat rather than one blended figure — and a code's
  minimum-spend threshold is measured against the cart's real total.
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
   `best_available`, sector = Galleria, and a one-entry zone map (Galleria €25) on *this*
   event. Point out that the same Galleria zone, on the New Year's Gala event, could carry
   a completely different price.
2b. **Price one tier by zone — the money shot.** Open the Platea `user_choice` tier and
   give it a price per painted zone (Platea Premium €80, Platea €45). Save, and the tier
   form reads the map back. Then delete one zone from the map and try to save again: the
   error names the zone you left out. That thirty-second loop is the whole pitch —
   *"the map isn't decoration, it's the price list, and the platform won't let you leave a
   hole in it."*
3. **Show that one mechanism spans both modes.** Still on Teatro Grande, open the seeded
   **"Platea — Best Available"** tier: same sector as the Platea user-choice tier, mode
   `best_available`, and a **two-zone** map (Platea Premium €80, Platea €45). Same map
   field, same paint, different way of choosing the seat. Contrast with Palco 1
   (`user_choice`, no map — flat €80 for the whole box).
4. **Show the buyer-side chart and availability** for that event:
   `GET /events/{event_id}/seating/chart` (the render-ready map: sectors, seats, painted
   zones) and `GET /events/{event_id}/seating/availability` (sparse — only sold /
   blocked / held seats are listed, everything else is free).
5. **Trigger a best-available hold** against the Galleria tier:
   `POST /events/{event_id}/seating/holds/best-available` with `tier_id`, `quantity`
   (try 4 to show adjacency), the `price_category_id` of the Galleria zone, and optionally
   `accessible_required: true` on a second call to show the accessible pool switch. Compare
   the two seat blocks returned.
5b. **Show the zone guardrails in three calls.** Repeat that hold (a) with
   `price_category_id` omitted → **400** listing the tier's zones; (b) with the *Palco*
   zone's id → **400**, because Palco isn't one of this tier's zones; (c) against the
   two-zone "Platea — Best Available" tier with each zone in turn → two different blocks
   at two different prices, from the same tier.
6. **Show the "sold out block" case** by requesting a quantity larger than any remaining
   contiguous run — the response is a clean 409 rather than a scattered assignment.
7. **For the comedy-club contrast**, switch to **The Chuckle Cellar**: front tables sold
   `user_choice`, riser sold `best_available` (one zone, €15), standing room sold `none`
   with a 40-person hard cap. Good for prospects who think "seating" only means theatres.

## Talking points & FAQs

**"Can the buyer pick their exact seat and pay a price that depends on where it is?"**
Yes — that's a `user_choice` tier with a zone price map. One tier, one map, a price
per painted zone. A cart mixing zones is a single checkout with the correct total, and
every ticket records its own price.

**"Can we do that without making people read a seat map?"**
Yes — same map, `best_available` mode. The buyer picks the **zone** ("2 in the Galleria")
and the system finds the best block inside it, at that zone's price. It's the same
`category_prices` field either way; only who picks the seat changes.

**"Can we charge students less for the same seats as everyone else?"**
Yes — that's what shared price zones are for. Create a second tier (e.g. "Platea
Student") pricing the same Platea zone as the adult tier, lower. Both tiers draw from the
identical seat pool; there's no seat duplication and no separate map to maintain. It works
in both modes and across multi-zone maps: the student tier gets its own map, priced lower
across the board.

**"Can we cap a zone — 50 Premium, 200 Standard?"**
Yes, with one tier per capped zone: each gets a single-entry zone map and its own
`total_quantity` (and, if you like, its own sales window). What you can't do is put two
separate counters inside one tier — see
[per-zone capacity](#per-zone-capacity-give-the-zone-its-own-tier).

**"We repriced a zone mid-run. What happens to tickets already sold?"**
Nothing — they keep the price they were sold at. Each ticket records what was actually
paid at the moment of sale, so a reprice changes future sales only and never rewrites the
revenue report for past ones.

**"What if we paint a new price zone after the tier is live?"**
Painting always works — it's venue-wide and never blocked by one event's setup. On a
`user_choice` tier, seats in a zone the tier doesn't price stop selling (a clear message
naming the zone, on the web and at the door) rather than quietly selling at the flat price;
the tier's admin screen flags the gap and adding a price fixes every future sale. On a
`best_available` tier there's nothing to fix: an unpriced zone simply isn't one of that
tier's zones. Either way the paint screen tells you, before you commit, which live tiers
the repaint would reprice. Worth saying out loud in a demo: "we'd rather refuse the sale
than charge the wrong price."

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
like a GA tier. The single exception is PWYC + zone pricing, which are mutually
exclusive by design.

**Honest limits:**
- `best_available` cannot split a party across rows — if the party doesn't fit in one
  contiguous run anywhere in the tier's sector-and-zone pool, it's a hard no, not a
  partial or best-effort placement.
- Neither seated mode can be pointed at a standing sector — there's nothing to assign.
  Standing sectors are `none`-mode only, gated by a head-count cap.
- **There's no per-zone capacity counter inside a tier.** A tier's `total_quantity` spans
  all of its zones — you can't say "50 Premium and 200 Standard" in one tier. The
  supported pattern is one tier per capped zone, each with a single-entry map and its own
  `total_quantity` (see
  [per-zone capacity](#per-zone-capacity-give-the-zone-its-own-tier)).
- **Best-available always needs an explicit zone on a mapped tier**, even a single-zone
  one. That's a client requirement, not a buyer-visible one — the UI sends the zone behind
  a "Galleria" button — but an integrator writing against the API must know it.
- **Reseating stays inside one price zone.** Moving a guest from Stalls to Balcony is
  a refund plus a new sale, not a reseat — see [`box-office.md`](box-office.md).
- **A price is quoted at checkout, not locked at hold time.** If an organizer repriced a
  zone in the seconds between a buyer opening the map and paying, the buyer is charged the
  new price. Card buyers see the amount on Stripe's page before paying; instant-issue
  paths return the per-ticket price on the ticket itself.
- Best-available assignment (both hold and purchase) has no manual "prefer this exact
  row" override for the buyer — it optimizes automatically. Manual seat control at that
  level of precision is a box-office (staff) action, not a buyer self-service one — see
  `box-office.md`.

## Under the hood

For the technically-curious rep or a prospect's technical evaluator:

- **Models**: `TicketTier` (`src/events/models/ticket.py`) carries `price`,
  `seat_assignment_mode` (`SeatAssignmentMode.NONE` / `USER_CHOICE` / `BEST_AVAILABLE`),
  optional FKs to `venue` and `sector`, and the `category_prices` map. **There is no
  `price_category` FK on the tier** — it was removed when the map became the sole pricing
  mechanism, so a tier never "has" one category. `PriceCategory`
  (`src/events/models/venue.py`) is venue-scoped (`name`, `color`, `display_order`) —
  its own docstring states the split plainly: *"Category lives on the map; price lives on
  the event via TicketTier. Multiple tiers MAY reference the same category (shared seat
  pool)."*
- **Zone prices**: `TicketTier.category_prices` is a JSON map of
  `{price_category_id: price}` on the tier itself (money stored as decimal strings —
  never floats), used by **both** seated modes. Being a plain field is why it survives
  event duplication and recurring occurrence generation for free. Parsing and write-time
  validation live in `src/events/utils/tier_pricing.py`; the checkout-side resolver is
  `src/events/service/seating/pricing.py` (`resolve_seat_price` /
  `build_batch_pricing`), which is also the single authority for per-ticket discount
  math and for refusing a seat in an unpriced category.
- **Zone selection**: `resolve_requested_zone` in
  `src/events/service/seating/pick.py` is the single authority for the request-time zone —
  called by the hold route, authenticated checkout and guest checkout alike, so the rule
  can't drift between them. It raises `InvalidZoneSelectionError` (400) for a missing,
  unknown, or non-applicable `price_category_id`, and returns the zone that
  `load_candidates` then intersects with `tier.sector_id`.
- **What the buyer's app reads**: tiers expose `seat_pricing` — *server-resolved*
  effective prices per zone plus the `unpainted` fallback, with `available: false` on
  any category the tier doesn't price. The frontend never re-derives prices from the raw
  map, so the price shown and the price charged cannot drift.
- **Validation**: `TicketTier._validate_venue_sector` and
  `_validate_seat_assignment_mode` (same file) enforce the per-mode requirements
  server-side at `clean()` time — **either** seated mode without a `sector_id`, or pointed
  at a standing sector, is rejected before save. `tier_pricing.validate_category_prices`
  covers the map itself (shape, venue scope, PWYC exclusivity, the ONLINE floor) and
  deliberately **nothing that depends on the paint**: a save-time rule may only read state
  the save controls, and paint is venue-wide (`paint_seats` never fails by design), so a
  coverage rule there could not prevent an uncovered tier — only break the next write to
  it, including `duplicate_event` and background occurrence generation. Coverage is
  reported instead: `pricing_gaps` and the seat-paint advisory's `missing_categories` for
  painted-but-unpriced (user-choice only — flagging it on a deliberately-scoped
  best-available tier would be a permanent false alarm), and `unsellable_zones` for the
  converse, priced-but-unpainted (best-available only, where a zone is selectable). The
  money guard is `resolve_seat_price`'s 400 at the till.
- **Best-available scoring**: the pure scoring function lives in
  `src/events/service/seating/best_available.py` (`pick_best_available` /
  `_pick_general` / `_pick_accessible`) — row order, centrality, fragmentation penalty,
  sector order, in that priority, with a seeded shuffle only across genuinely-tied
  placements. Accessible seats are filtered out of `_pick_general`'s input entirely,
  which is what makes the protection unconditional rather than a fallback.
- **Candidate loading**: `src/events/service/seating/pick.py` (`load_candidates`,
  `hold_best_available`) is the DB-facing half — it confines the pool to the tier's sector
  and (when given) the requested zone, excludes sold/held/overridden/inactive seats,
  computes each row's real physical bounds (so centrality scores against the true midpoint,
  not the shrinking available pool), and retries the pick up to 3 times if a chosen seat is
  taken between the unlocked read and the lock.
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
  Seating"](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md#journey-19-venue--seating), particularly §19.3
  (tier seating configuration) and §19.5 (best-available buying flow).
