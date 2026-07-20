# Venue & Layout Setup

## What it is & who it's for

Revel lets an organizer build a **reusable digital floor plan** of a physical venue — sectors,
individual seats, and named price categories — once, then attach it to as many events as they
like. It is the foundation the rest of the seating stack (tier configuration, seat selection,
box office) is built on: no layout, no seat picker.

It fits venues of very different shapes:

- **A ~120-seat comedy club** (tables + a small standing area at the back)
- **A ~700-seat music hall** (GA floor + a seated balcony)
- **A ~1,350-seat theatre** (orchestra, balcony, private boxes)

It also fits venues that are pure general admission — a venue with no seats at all is a
perfectly valid setup. Seating is opt-in complexity: an organizer only builds a layout if they
actually need reserved seats.

## How it works

### Venues: the reusable container

A **venue** belongs to one organization and is not tied to a single event. It has a name,
address/city, an optional description, and a **soft capacity**. "Soft" means: if an event sets
its own `max_attendees`, the effective cap is the *smaller* of the two numbers — the venue
capacity is a ceiling, not a separate hard rule enforced seat-by-seat (that job belongs to
sectors and seats, below).

Because a venue is a standalone object, an organizer builds it **once** and links it to every
event that happens there — a weekly comedy night reuses the same club layout fifty-two times a
year without recreating a single seat.

### Sectors: the logical areas, and their kind

Inside a venue, a **sector** is a named area — "Platea," "Balcony," "Standing Room." Every
sector has a `kind`:

- **`seated`** — holds individual, materialized seats. Its effective capacity is simply however
  many seats exist in it.
- **`standing`** — no seats at all. It carries its own hard `capacity` number instead, which is
  the ceiling for general-admission tickets sold against it.

A sector can also carry an optional **shape** (a polygon of points) and a **display order**, both
purely for how the venue map renders on screen.

**Guardrail:** a sector's kind can only be changed while it has **zero seats**. Try to flip a
seated sector to standing (or vice versa) while seats exist, and the API refuses the change with
a plain 400 error — "delete its seats first." This exists so a sector never silently loses its
seat-level detail (and the tickets that reference it) out from under an event. In practice this
means: decide `seated` vs `standing` early, or clear the sector before switching.

### Seats & the grid editor

For a `seated` sector, seats are individual rows in the database — a `label`, an optional
`row_label` and `number`, a screen `position`, and three flags: `is_accessible`,
`is_obstructed_view`, and `is_active` (a decommissioned seat — pulled from sale without deleting
it, e.g. a broken chair).

Seats are managed in bulk through what the organizer sees as a **grid editor**:

- **Bulk create** — lay out an entire row (or a whole sector) in one call
- **Bulk update** — re-label, re-flag, or repaint many seats by their label in one call
- **Bulk delete** — remove seats by label, all-or-nothing (if even one seat in the batch is
  referenced by a ticket or an active hold, *nothing* is deleted — no partial, confusing state)
- **Single seat update/delete** — for one-off fixes

A seat's **label is permanent**. Renaming means delete-and-recreate — there is no "rename"
endpoint. And a seat that's ever been on a ticket, or is currently on hold, can't be hard-deleted
at all; it must be decommissioned (`is_active = false`) instead. This protects historical ticket
data — a deleted seat could otherwise orphan a past attendee's ticket record.

If the sector has a shape, every seat's `position` must physically sit inside that polygon — the
API will reject a seat placed off the map.

### Natural row ordering — best-available "just works"

This is the detail that makes the rest of the seating stack (best-available assignment,
front-and-center picks) work without any manual setup: **the platform derives, for every seat, a
front-to-back row rank and a left-to-right position in the row — automatically.**

- Rows are ranked front-to-back using **natural order** on the row label, not plain alphabetical
  order. That means row "2" sorts before row "10" (not after, as plain string sort would do), and
  in venues that continue past "Z" with double letters, "Z" sorts before "AA" — the standard
  theatre convention.
- Within a row, seats are ranked left-to-right by their printed `number`, then by `label` as a
  tiebreaker for unnumbered seats.
- This re-ranking runs automatically on seat create, bulk-create, and bulk-update, whenever a row
  or number changes — the organizer never has to think about it.

**The escape hatch:** if an organizer's real-world layout doesn't follow either convention (an
oddly shaped room, seats numbered right-to-left, etc.), they can supply an explicit `row_order`
and `adjacency_index` on any seat in a request. Doing so on **even one seat** switches off
automatic derivation for that entire request — explicit values always win, wholesale. This is the
manual override for the rare venue that needs it; every other venue never touches these fields.

### Price categories: named, colored, painted onto seats

A **price category** is a small, venue-scoped object: a `name`, a hex `color` (for the seat map),
and a `display_order`. It carries **no number of its own** — the money always lives on the event's
ticket tier (see [tiers-and-pricing.md](tiers-and-pricing.md)). What the category *is* is the
label a tier prices: "Orchestra," "Premium," "Balcony" as buckets of similarly-desirable seats,
independent of which event is selling them today.

Two ways a tier turns that label into money, both covered in the tiers guide: a
`best_available` tier draws its seat pool from one category at one flat price, and a
`user_choice` tier can carry a **price per category**, so the buyer's own click decides what
they pay. Either way, painting is what tells the platform which seats are which.

- Full CRUD lives under the venue. A duplicate category name on the same venue is rejected (400).
- **Painting** is how a category gets attached to seats: `PUT .../venues/{id}/seats/paint` takes
  a list of seat ids and a category id (or `null` to erase paint) and applies it in a single bulk
  update — paint a whole row, or an entire sector, in one action.
- **The paint round-trip works end-to-end**: when the admin grid reloads, every seat comes back
  with its current `price_category_id` and the category's name/color already attached, so the map
  redraws with the right colors — no re-painting on every visit.
- **Delete guard**: a category currently used by a ticket tier cannot be deleted — the API refuses
  with a clear 400 that **names the events and tiers** still using it, whether they use it as a
  best-available seat pool or price it in a category map. Reassign or reprice those tiers first.
  Seats that were merely *painted* with the category are unaffected by a delete: their paint just
  clears to "unpainted" and can be repainted with something else.

> **Repainting is venue-wide and takes effect immediately — for every event at this venue.**
> This used to be a cosmetic change: repaint a block and its outline colour changed on every
> map. Now that a `user_choice` tier can price seats by category, **repainting can change what
> buyers are charged**, at every event in that building, the moment it's saved. There is no
> per-event paint and no scheduled paint.
>
> Painting deliberately never fails, even when it leaves an event's tier with a category it
> doesn't price — blocking a venue-wide edit because of one show's pricing setup would be worse.
> The consequence lands elsewhere: seats in an unpriced category **stop selling** on that tier
> (with a message naming the category) until the organizer prices it. Sales in priced categories
> are unaffected.
>
> Moving seats between two categories a tier **does** price is the quieter hazard: coverage stays
> complete, nothing errors, and the seats simply start selling at the other price. So the save
> itself reports back — it names every live tier whose seat prices it just changed, the event each
> belongs to, and how many seats moved from which price to which. That's the only place the
> venue-wide blast radius is visible, since the change spans events the organizer isn't looking at.
>
> Practical advice for organizers running live on-sales: repaint before a show goes on sale, and
> read what the save reports back. The tier admin screen also flags any category it doesn't price.

## Demo it

A good live demo takes under five minutes and uses venues that already exist in the seeded
demo/showcase data (`make seed` creates them automatically): **Teatro Grande** (a ~1,350-seat
theatre — Platea, Galleria, and four private Palco boxes), **The Chuckle Cellar** (a 120-seat
comedy club — front tables, a small riser, and a 40-person standing area), and **Mittelfest
Halle** (a ~720-seat hall — a 600-capacity GA floor plus a 120-seat balcony). Pick whichever
matches the prospect's own room.

| Venue | Capacity | Sectors (kind) | Price categories |
|---|---|---|---|
| Teatro Grande | ~1,400 | Platea (seated, 748 seats), Galleria (seated, 572 seats), Palco 1–4 (seated boxes, 8 seats each) | Platea Premium, Platea, Galleria, Palco |
| The Chuckle Cellar | 120 | Front Tables (seated, 32 seats), Riser (seated, 48 seats), Standing Room (standing, capacity 40) | Front Tables, Riser (Standing Room has none — it's pure GA) |
| Mittelfest Halle | 720 | Floor (standing, capacity 600), Balcony (seated, 120 seats) | Balcony (Floor has none — it's pure GA) |

**Admin UI walkthrough:**

1. Go to `/org/[slug]/admin/venues` and open one of the showcase venues — say, **Teatro Grande**.
2. Show the venue detail: name, address, soft capacity (1,400), and its sectors listed with
   display order.
3. Open the **Platea** sector — point out `kind: seated`, its shape rendering as a polygon on the
   map, and its capacity being simply "however many seats exist" (748 in the real seed data,
   across 22 rows).
4. Open the grid editor. Show a *bulk create* of a fresh row — a handful of seats with just a row
   letter and numbers, no manual ordering — then show they land in the right front-to-back /
   left-to-right position automatically. This is the moment to say the words "natural row
   ordering, no manual setup."
5. Flip to the **Standing Room** sector (in The Chuckle Cellar) or **Floor** (in Mittelfest
   Halle) to show the `standing` kind: no seats, just a single hard capacity number.
6. Try (and let it fail) changing a seated sector's kind while it has seats — show the plain
   400 error. This is a good moment for the honest "guardrail, not a bug" framing.
7. Open **Price Categories** on the venue: show the CRUD list (e.g. Teatro Grande's "Platea
   Premium," "Platea," "Galleria," "Palco" — each with its own color), then multi-select a block
   of seats on the grid and paint them with a category in one action.
8. Reload the page and show the paint is still there — the round-trip.
9. Try deleting a category that's in use by a tier and show the refusal message, then show an
   unused category deleting cleanly.

**Underlying endpoints touched in this walkthrough** (all under
`/organization-admin/{slug}/venues/...`, staff-authenticated):

| Step | Endpoint |
|---|---|
| List/create/edit venues | `GET|POST /venues`, `PUT /venues/{venue_id}` |
| List/create/edit sectors | `GET|POST /venues/{venue_id}/sectors`, `PUT .../sectors/{sector_id}` |
| Bulk create/update/delete seats | `POST .../sectors/{sector_id}/seats`, `PUT .../seats/bulk-update`, `POST .../seats/bulk-delete` |
| Update/delete one seat | `PUT|DELETE .../seats/by-label/{label}` |
| Price categories CRUD | `GET|POST /venues/{venue_id}/price-categories`, `PUT|DELETE .../price-categories/{category_id}` |
| Paint seats | `PUT /venues/{venue_id}/seats/paint` |

## Talking points & FAQs

**"Can we reuse a venue across every show in our season?"**
Yes — that's exactly what a venue is for. Build the layout once, link it to as many events as
you like. A weekly comedy night doesn't re-draw its floor plan every week.

**"Do we have to use seats at all?"**
No. A venue can be entirely GA — one or more `standing` sectors with a capacity number, no seats
anywhere. Reserved seating is opt-in; small venues and GA-only shows aren't forced into a seat
map they don't need.

**"What if we get the sector type wrong — seated vs. standing?"**
You can change it, but only while the sector has no seats yet. Once seats exist, the system
refuses the switch outright with a clear error, rather than silently deleting seat data. The fix
is simple (delete the seats, then switch), but it's worth deciding sector type early, or building
in a test sector first if unsure.

**"How much manual setup does row/seat ordering take for a 1,000+ seat theatre?"**
None, in the common case. Give each seat a row label and a number and the platform figures out
front-to-back and left-to-right order for you — including the tricky bits (row "2" before row
"10"; theatre rows that continue "…Y, Z, AA, AB…"). This is what makes automatic "best available,
front and center" picks possible later without a human ranking every seat by hand. If a venue's
layout is genuinely irregular, an explicit override is available — but it's the exception, not
the default workflow.

**"What happens if we delete a price category that's actually being sold?"**
It's blocked, and the error lists the events and tiers standing in the way. That covers both
uses: a best-available tier whose seat pool is that category, and a user-choice tier that puts a
price on it. Either would otherwise break silently mid-sale — the second one by collapsing an
€80 zone back to the tier's flat price with nothing anywhere reporting it.

**"What happens to seats when we do successfully delete a category?"**
The seats that were painted with it simply become unpainted (no category) — they aren't deleted
or broken, and can be repainted with a different category any time. Unpainted seats sell at the
tier's flat price.

**"If we repaint the map, does it affect shows that are already on sale?"**
Yes, immediately and everywhere — the map belongs to the venue, not to one event. When a
`user_choice` tier prices seats per category, a repaint moves seats between price zones for
every event at that venue at once. Painting itself always succeeds (we won't let one show's
pricing block your map work), but seats painted into a zone a live tier doesn't price stop
selling until it's priced, rather than quietly selling at the wrong price. Repaint between
on-sales where you can.

**"Can we rename a seat, like fixing a typo in its label?"**
Not directly — seat labels are permanent once created. The fix is delete-and-recreate. This is a
deliberate, honest limitation: labels are the identifier tickets reference, so we don't allow them
to silently drift.

**"Can we delete a seat that already sold a ticket?"**
No — any seat that has ever appeared on a ticket (regardless of that ticket's status) or is
currently on an active hold cannot be hard-deleted. Decommission it instead (mark inactive); it
stays in the historical record but drops out of the sellable pool. This is a real, current
constraint, not a future promise.

**"How big a venue can this actually handle?"**
The seed data includes a ~1,350-seat theatre with three sector types (open floor, tiered balcony,
private boxes) as a working reference point. There is no seat-count ceiling in the design.

**"Is there version history if we redraw the map?"**
No — this is a known v1 limitation, worth saying plainly rather than glossing over: the seating
chart is served straight from the live tables, with no snapshot/versioning layer yet. Changing a
layout changes it going forward; there's no "map as it looked on show night" replay.

For how a painted price category turns into an actual ticket price and seat-assignment mode, see
[tiers-and-pricing.md](tiers-and-pricing.md). For what the buyer sees on the seat picker, see
[buyer-experience.md](buyer-experience.md). For how event-day staff override or hand-sell
specific seats, see [box-office.md](box-office.md).

## Under the hood

Venue, sector, and seat data lives on three models — `Venue`, `VenueSector`, `VenueSeat` — with
CRUD served entirely from `OrganizationAdminVenuesController`
(`/organization-admin/{slug}/venues/...`), and the actual business logic (sector-kind guardrail,
seat rank derivation, category paint/delete rules) living in `venue_service.py` — controllers stay
thin and just wire requests through. The natural-order derivation is the
`derive_sector_seat_ranks()` / `natural_row_key()` pair, re-run automatically whenever seat rows or
numbers change unless a request supplies explicit ranks. Paint is a single bulk `UPDATE` via
`paint_seats()`. For the full technical narrative — including how this setup step feeds tier
configuration, buyer seat selection, and box-office control — see
[https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md) §19.1 (Venue & Layout Setup) and §19.2
(Price Categories).
