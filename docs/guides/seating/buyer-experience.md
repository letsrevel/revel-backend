# The Buyer Experience

Revel's seating engine lets a buyer see a real seat map, pick (or auto-pick) exact seats, and
buy them — with a server that guarantees no two people ever walk away with the same seat. This
page covers what the buyer sees and does, from the map to the ticket in their hand.

## What it is & who it's for

Any organizer running a seated event — Teatro Grande's 2,500-seat main hall, The Chuckle
Cellar's 120-seat room, a single reserved block at Mittelfest Halle — can turn on a seat map for
a ticket tier. Buyers then choose exact seats instead of an anonymous "1x General Admission."

Three ways a tier can sell seats, set by the organizer per tier:

- **General admission (`NONE`)** — no seat map, just a quantity. Optionally capped by a standing
  sector's hard capacity (e.g. "300 people on the floor, no seats").
- **User-choice** — the buyer picks their own seats from the map, like buying cinema tickets.
  One price for the whole tier, or a price per painted zone so the seat the buyer clicks decides
  what they pay.
- **Best-available** — the buyer just says "2 tickets," and the system finds them the best pair
  together. No map-reading required.

This page is about the last two — anywhere a buyer interacts with actual seats. It is not a
walkthrough of the organizer-side venue/tier setup (see `venue-and-layout.md` and
`tiers-and-pricing.md`) or of door-side seat control (see `box-office.md`).

**Honest scope note**: the buyer-facing map and picker UI is being built by the frontend team.
Everything below is the flow and the API it will call — useful for demoing the mechanics and
answering "what happens when..." questions today, even before the polished UI ships.

## How it works

### The availability map

The buyer sees a seat map with three non-available states:

- **Sold** — someone already has a (non-cancelled) ticket on that seat. This includes checked-in
  attendees — a seat doesn't free up just because the person already walked in.
- **Held** — someone else is actively holding it right now (see holds, below).
- **Blocked** — the organizer's box office pulled it out of sale (comp/house/tech hold, or the
  seat was decommissioned entirely).

Every other seat is available. The map is **sparse** — the server only reports the seats that
are *not* available; anything missing from that list is fair game. This keeps the payload small
even for a 2,500-seat hall where 2,400 seats are still open.

Standing sectors (no individual seats) show a running count instead: capacity and how many are
taken, so a "300 on the floor" GA sector can show "214 / 300" on the map.

If a seat qualifies for more than one state at once — say it's sold *and* someone still has a
stale hold on it — **sold wins**, then blocked, then held. The buyer's own held seats are also
returned separately, along with the earliest expiry across all of them, so the UI can show a
countdown.

This is a **live, refresh-on-poll view** — not a permanent snapshot. The buyer's app re-fetches
it periodically (and after every hold/release) so the map stays honest as other buyers act. The
availability response also echoes the layout's version, so an app polling for hours notices when
the organizer has redrawn or repainted the room and pulls a fresh map — which matters more now
that paint can decide price, not just colour.

### Prices on the map

When a tier prices seats by zone (see [`tiers-and-pricing.md`](tiers-and-pricing.md)), the map
stops being a colour-coded picture and becomes a price list:

- **A price legend per zone.** Each painted category comes back with its name, its colour and
  **the price a buyer will actually be charged** for a seat in it — plus the price of an
  unpainted seat, if the room has any. The buyer reads "Premium €80 / Stalls €50 / Balcony €30"
  next to the same colours they see on the seats.
- **A running total as they pick.** Because every seat's price is known up front, a cart of
  "A-7 (Premium) + M-22 (Stalls)" shows €130 before the buyer commits — no "prices calculated at
  checkout" surprise.
- **Unsellable seats are greyed out, not mispriced.** If the organizer painted a zone the tier
  doesn't price yet, that zone comes back in the legend flagged unavailable and with no price —
  so the app greys out its seats instead of inventing a number. Checkout refuses them anyway,
  with a message naming the zone. Seats in priced zones sell normally.

The prices in the legend are **computed by the server**, not assembled by the app from raw
configuration. That is deliberate: it is the only way to guarantee the number a buyer is shown is
the number they are charged.

### What the buyer is charged, and when

The price is settled **at checkout**, not when the seat is held. Holds reserve a seat, not a
price.

In practice this is invisible — an organizer repricing a zone in the ninety seconds a buyer
spends choosing is a rare event — but be honest about it if a prospect asks:

- **Card buyers** see the real amounts on Stripe's checkout page, itemised per seat, before they
  pay anything.
- **Free, offline and at-the-door tickets** are issued instantly with no confirmation screen, so
  the ticket itself carries the per-seat price the buyer was charged; the buyer's app shows it on
  the ticket.
- Everything downstream agrees: each ticket records its own price, a discount code is applied
  per ticket rather than as one blended figure, and the VAT preview (for buyers who need a
  business-shaped number before paying) takes the chosen seats into account. That preview is a
  quote, not a reservation — it deliberately doesn't check whether the seats are still free, so
  a buyer can price a cart without holding the room hostage.

### Seat holds (the cart)

Tapping a seat isn't just a UI selection — it's a real, server-side **hold**: a short-lived
reservation that keeps that seat off other buyers' maps while this buyer decides.

- **10-minute TTL.** A hold expires automatically if the buyer walks away.
- **Auto-refresh, capped at 30 minutes.** Re-requesting seats you already hold refreshes the
  timer — a buyer actively working through a purchase doesn't get timed out mid-flow — but the
  refresh is bounded to 30 minutes from the *first* time they grabbed the seat, so a hold can't
  be kept alive forever.
- **All-or-nothing.** A request for 4 seats either holds all 4, or holds none of them. A buyer
  never ends up with a partial, mismatched hold.
- **Capped per buyer.** By default a buyer can hold up to 10 seats on one event at a time
  (organizers can raise or lower this per event); go over it and the request is rejected outright
  rather than holding the first few and dropping the rest.

When a hold request can't be satisfied, the buyer gets one of three plain-English outcomes:

| Outcome | What happened | What the buyer should do |
|---|---|---|
| **`capacity`** | They're already holding as many seats as this event allows. | Reduce how many seats they're asking for — not "pick different seats," the limit is on quantity. |
| **`unavailable`** | One or more of the requested seats are sold, blocked, or actively held by someone else. | Refresh the map and re-pick — those specific seats are gone. |
| **`no_block`** *(best-available only)* | There's no run of adjacent seats big enough for the party size. | Try a smaller party size, or check if standing/GA is an option. |

**Holds are advisory UX, not the safety net.** They make the map feel responsive and stop two
people from fighting over the same seat mid-browse, but the actual guarantee against double
selling lives at the database level, enforced again at the moment of purchase. Losing your hold
(TTL expiry) never means losing a *ticket* you already paid for — it only means someone else
might grab the seat before you check out.

### User-choice checkout

The buyer picks exact seats on the map, holds them, then checks out. Purchase consumes their own
holds on those seats and turns them into tickets. If someone else's live hold or ticket beats
them to a seat between holding and paying, checkout rejects just that seat with a clear conflict
— never a silent overwrite.

### Best-available checkout

The buyer doesn't pick seats at all — they say how many tickets they want (and, optionally, that
they need accessible seating), and Revel finds them the best block together:

- **Front rows first**, then seats closest to the middle of the row, then a check that it doesn't
  leave a single stray empty seat stranded next to the block (so the room stays sellable as it
  fills up).
- The system holds that exact block and shows the buyer the real seats it picked — row and seat
  numbers, not just "2 tickets" — *before* they pay.
- **What you saw held is what you buy.** Checkout consumes the exact seats that were held for
  best-available — it does not silently re-run the picker and potentially hand the buyer a
  different (if equally good) block. A best-available purchase also works even without a prior
  hold step (the same picker runs at the moment of purchase), which matters for any checkout path
  that skips the map screen.

### Guest checkout (no account)

None of this requires an account. An anonymous visitor holds seats exactly like a logged-in
buyer — the first hold request silently mints a signed, httpOnly session cookie behind the
scenes, and every hold after that is tied to it.

The one real wrinkle is **free, offline, and at-the-door tiers**, which confirm by email link
rather than instantly:

1. Guest picks seats (or requests best-available) and hits "Buy as Guest."
2. Revel emails a confirmation link instead of issuing the ticket immediately.
3. Clicking the link is what actually assigns/consumes the seats and creates the ticket.

Because step 3 can happen on a **different device** than step 1 — think "I held seats on my
phone in the lobby, then confirmed from my laptop's inbox at home" — the buyer's hold identity
and their accessible-seating preference are baked directly into the signed confirmation link
itself. So opening the email on another device still honors the seats that were held earlier,
rather than treating them as a stranger's holds. **Online (card) guest checkout skips this
entirely** — it goes straight to Stripe, no email round-trip, so there's no cross-device
question for paid tiers.

### Accessible seating for buyers

A buyer can flag that they need an accessible seat. Accessible seats are kept in a protected
pool that ordinary best-available requests never touch — so a wheelchair-accessible seat can't
be accidentally swept up by a general party of two grabbing "best available." When a buyer *does*
ask for accessible seating, best-available draws only from that protected pool (and relaxes the
"must be side-by-side" rule slightly, since accessible seats are often placed individually rather
than in a row). If there simply aren't enough accessible seats left, the buyer gets a clear
message to contact the organizer directly — no dead end, no silent failure. This works in the
guest email-confirm flow too, for the same free/offline/door tiers described above.

## Demo it

The map/picker UI itself is still being built by the frontend team, so demo this as the flow +
API a prospect's developer (or a technical prospect) can see working today. Framed as what a
buyer's screen would show at each step:

1. **Buyer opens the seat map.**
   `GET /events/{event_id}/seating/chart` — the full venue layout: sectors, every seat, and the
   price categories (colors) painted onto them. This is what draws the picture. The prices come
   from the event's tiers: a tier that prices seats by zone returns a `seat_pricing` block — the
   effective price of each zone plus the unpainted fallback — which is what the legend renders.
   A flat tier returns nothing there, and the app shows one price, exactly as before.

2. **Buyer sees what's taken.**
   `GET /events/{event_id}/seating/availability` — the sparse sold/held/blocked map, standing
   counts, and (if the buyer already has holds) their own held seats + countdown. The frontend
   polls this to keep the map current.

3a. **Buyer taps seats themselves (user-choice).**
   `POST /events/{event_id}/seating/holds` with the chosen seat ids. A 200 means they're held,
   with an `expires_at` for the countdown; a 409 means a conflict, with the reason table above.

3b. **Or buyer says "just get me good seats" (best-available).**
   `POST /events/{event_id}/seating/holds/best-available` with the tier, a quantity, and an
   optional accessible flag. 200 returns the exact seats picked (show them on the map); 409 with
   `no_block` means that party size doesn't fit anywhere right now.

4. **Buyer changes their mind.**
   `DELETE /events/{event_id}/seating/holds` releases their holds (all, or a subset by seat id)
   — e.g. an "undo" or a seat swapped back out before paying.

5. **Buyer checks out.**
   - Logged in: `POST /events/{event_id}/tickets/{tier_id}/checkout` (or `/checkout/pwyc` for
     pay-what-you-can tiers).
   - Guest: the `/public` twin of each of those, e.g.
     `POST /events/{event_id}/tickets/{tier_id}/checkout/public`.
   - **Online (card) tier**: response comes back `requires_payment=true` with a
     `reservation_id`; the buyer's app then calls
     `POST /events/reservations/{reservation_id}/checkout-session` (or `.../public` for a guest)
     to get the Stripe checkout URL and hands the buyer off to pay.
   - **Free / offline / at-the-door tier, logged in**: the ticket is issued immediately in this
     same call — no extra step.
   - **Free / offline / at-the-door tier, guest**: the buyer instead sees "check your email to
     confirm" — clicking that link calls `POST /events/guest-actions/confirm`, which is the
     moment the seats are actually assigned/consumed and the ticket is created.

Good demo beats: show a 409 on the same seat from two browser tabs (the "no double-sell"
guarantee, live); show a best-available pick's exact row/seat numbers before paying; show the
accessible-required toggle skipping straight to accessible rows.

## Talking points & FAQs

**"Nobody can ever buy the same seat twice."** The hold system makes the map feel instant and
fair, but the real guarantee is enforced again, atomically, at the moment of payment. Even if two
buyers' holds somehow raced, only one purchase can ever succeed on a given seat.

**"Buyers get the exact seats they saw, not a surprise."** Best-available shows the real seats
before the buyer pays, and checkout locks in precisely that block — never a different
"equally good" substitute picked behind the scenes.

**"The price on the map is the price they pay."** Zone prices are resolved by the server and
handed to the app ready to display, so the legend, the running total, the Stripe page and the
ticket are all the same number. Nothing is recomputed client-side, which is where price drift
usually comes from.

**"No account required to browse or hold seats."** A prospect worried about checkout friction can
let anonymous visitors explore the map and even hold seats — login/email is only needed at the
final purchase step.

**"Accessible seating isn't an afterthought."** Accessible seats are structurally protected from
general best-available assignment, so an organizer doesn't have to manually manage a waiting
list or manually override sales to keep accessible seats free for the people who need them.

**FAQ — What happens if two people tap the same seat at the same instant?**
One hold wins; the other gets a 409 `unavailable` immediately and is told to refresh and re-pick.
No two holds are ever granted on the same seat.

**FAQ — My hold expired before I paid — did I lose my ticket?**
No ticket existed yet — a hold is just a reservation on the map, not a purchase. Worst case, the
buyer has to re-pick if someone else took the seat in the meantime; nothing charges or fails
silently.

**FAQ — Can a buyer hoard seats by holding way more than they'll buy?**
Holds are capped per buyer per event (10 by default, organizer-configurable), and the write
endpoints are rate-limited, so grabbing dozens of seats to squat on them isn't practical.

**FAQ — What if the venue has no seat map at all?**
General admission tiers need none of this — seating is entirely opt-in per tier. A GA-only event
never touches the chart/availability/hold endpoints.

**FAQ — Does this work for a 120-seat room the same as a 2,500-seat theatre?**
Yes — the same chart/availability/hold/best-available flow runs at both scales. The sparse map
keeps payloads small even at 2,500 seats; a room like The Chuckle Cellar just has a smaller chart
to render.

## Under the hood

Full behavioral spec: [`USER_JOURNEYS.md` §19](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md#journey-19-venue--seating),
particularly:
- [§19.4 — Buy Seated Tickets, User Choice](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md#194-buy-seated-tickets--user-choice-attendee)
- [§19.5 — Buy Seated Tickets, Best Available](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md#195-buy-seated-tickets--best-available-attendee)
- [§19.6 — Guest Best-Available & Holds](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md#196-guest-best-available--holds)
- [§6.7 — Seat Selection](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md#67-seat-selection) and
  [§7.4 — Guest Seated Checkout](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md#74-guest-seated-checkout)

Key endpoints (all under `/events`):

| Endpoint | Purpose |
|---|---|
| `GET /{event_id}/seating/chart` | Render-ready venue layout for the event |
| `GET /{event_id}/seating/availability` | Sparse sold/held/blocked map + standing counts + caller's holds |
| `POST /{event_id}/seating/holds` | All-or-nothing TTL hold on specific seats |
| `POST /{event_id}/seating/holds/best-available` | Optimistic hold of the best adjacent block |
| `DELETE /{event_id}/seating/holds` | Release the caller's holds (subset or all) |
| `POST /{event_id}/tickets/{tier_id}/checkout` (+ `/pwyc`, `/public`, `/pwyc/public`) | Purchase — consumes the buyer's own holds |
| `POST /reservations/{reservation_id}/checkout-session` (+ `/public`) | Fetch the Stripe checkout URL for an online-tier reservation |
| `POST /guest-actions/confirm` | Guest email-link confirm — where free/offline/door seats are actually assigned |

Related pages: `venue-and-layout.md` (how organizers build the map buyers see),
`tiers-and-pricing.md` (how a tier is wired to `NONE` / `USER_CHOICE` / `BEST_AVAILABLE`), and
`box-office.md` (how staff override, comp, and reseat around what buyers hold/own).
