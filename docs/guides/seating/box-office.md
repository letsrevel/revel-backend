# Box Office & Event-Day Operations

## What it is & who it's for

Every seated event needs a human at the door who can override the plan: hold the
house seats for VIPs, comp the critic, sell a walk-up a ticket for cash, and move
someone when their seat is broken. That's box office.

This is the layer that separates a real ticketing system from a spreadsheet with a
QR code bolted on. A prospect who runs a 120-seat comedy club will ask "what happens
when someone shows up without a ticket and pays cash?" A theatre press-night manager
will ask "how do I hold the front row for critics without selling it by accident?"
This page answers both.

Everything here requires the `manage_tickets` staff permission on the event's
organization (check-in itself uses the narrower `check_in_attendees` permission —
see [Check in and see the seat](#7-check-in-and-see-the-seat)). It only applies to seated
venues — see [`venue-and-layout.md`](venue-and-layout.md) for how a venue gets seats
in the first place, and [`tiers-and-pricing.md`](tiers-and-pricing.md) for how a tier
prices painted zones.

## How it works

Box office has four moving parts, all scoped to **this event** — none of them touch
the venue's master layout:

1. **Overrides** — hold or kill individual seats for this event only, with a reason.
2. **Door sales & comps** — issue a ticket directly onto a seat, bypassing the
   buyer-facing rules that protect a self-service checkout.
3. **Reseat** — move an existing ticket to a different free seat.
4. **Check-in with the seat** — the QR scan result now tells door staff which seat
   the attendee is in.

A few invariants run underneath all four, and they're worth knowing before you demo
this to a skeptical box-office manager:

- **Nothing here is a hack around capacity.** Door sales still check tier and event
  capacity — they only skip the buyer-facing restrictions (see below). You cannot
  oversell a sold-out venue from the door.
- **A ticketed seat can't be silently taken.** Overrides reject a seat that already
  has a live (non-cancelled) ticket on it — the box office can't kill or hold a seat
  out from under a paying guest.
- **Overrides are per-event.** Put a hold on seat C-12 for Friday's show and it has
  no effect on Saturday's show, or on the venue's layout in general. This is what
  lets a promoter block the same physical seats differently every night (house seats
  Friday, sold Saturday, broken Sunday).
- **Everything is row/PK-locked before it's checked**, so two staff members hitting
  "sell" on the same seat at the same door terminal can't both succeed.

## Demo it

All of the following are event-admin endpoints under
`/event-admin/{event_id}/seating/...`, gated by the `manage_tickets` permission.
Assume you're logged in as a staff member with that permission on the organization.

### 1. Hold the house seats

Before the show, block off the seats you never intend to sell:

```
PUT /event-admin/{event_id}/seating/overrides
{
  "set": [
    {"seat_id": "<house-seat-1>", "status": "held", "reason": "House seats — promoter"},
    {"seat_id": "<tech-booth-seat>", "status": "held", "reason": "Tech booth sightline"}
  ],
  "release_seat_ids": []
}
```

Walk the prospect through the response: `{"applied": 2, "released": 0, "rejected": {}}`.
Now pull up the buyer-facing seat map (`GET /events/{event_id}/seating/chart` +
`/seating/availability` — see [`buyer-experience.md`](buyer-experience.md)) and show
those two seats reading as **blocked**. A buyer can't pick them, and best-available
skips right over them.

### 2. Kill a broken seat

A seat's arm is broken, or there's a structural sightline issue for this one show:

```
PUT /event-admin/{event_id}/seating/overrides
{
  "set": [{"seat_id": "<broken-seat>", "status": "killed", "reason": "Broken armrest — maintenance ticket #482"}],
  "release_seat_ids": []
}
```

Point out: this seat is still perfectly fine in the venue's permanent layout — it's
only dead for *this event*. Next week's show sells it normally.

### 3. Sell a walk-up ticket at the door — cash

The Friday-night door line has a walk-up who wants to pay cash for the last open
seat:

```
POST /event-admin/{event_id}/seating/sell
{
  "seat_id": "<seat-id>",
  "tier_id": "<tier-id>",
  "payment_method": "at_the_door",
  "email": "walkup@example.com",
  "guest_name": "Walk-up Guest"
}
```

The ticket comes back **ACTIVE** immediately — no payment link, no waiting. A door
sale is a real, full-price sale and reports as one.

**How much did they pay?** It depends on how the tier is priced:

- **Flat-price tier** (no zone map) — the ticket reports at the tier's list price. Nothing
  to think about.
- **Tier with a zone price map** (either seat-assignment mode) — the ticket records **the
  price of that seat, at the
  moment of sale**. Sell an €80 Premium seat at the door tonight and it is booked as €80
  forever, even if the zone goes to €95 next week. That's the point: takings for a night
  that has already happened can't be rewritten by a later price change.

The operational consequence, worth saying to a box-office manager rather than letting
them find it: **the amount to collect is per seat now, so the door screen has to show
it.** If your staff are working from a printed price list instead, cash in the drawer and
the number in the report will drift.

One refusal to know about: if a seat has been painted into a price zone the tier's map
doesn't price, the door sale is **rejected** with a message naming the zone — the same
rule buyers hit online, and it applies to both seated modes. It is **never** quietly sold
at the tier's flat `price`: that fallback is exactly the silent mispricing the zone map
exists to prevent, and in the books a forced sale is indistinguishable from the bug. So
there's deliberately no staff override. The escape hatches are honest ones: comp the seat
(recorded as 0.00), price the zone on this tier (seconds, and fixes every subsequent
sale), or sell it on the tier that *does* cover that zone.

### 4. Comp the critic — press night

A critic is on the guest list for a comp seat, no charge:

```
POST /event-admin/{event_id}/seating/sell
{
  "seat_id": "<critic-seat-id>",
  "tier_id": "<tier-id>",
  "payment_method": "free",
  "email": "critic@thepaper.example"
}
```

`price_paid` comes back as `0.00` — the revenue report will never inflate takings
with comp seats. If `critic@thepaper.example` already has an account (e.g. they
bought a ticket to a previous show), the ticket attaches to that account instead of
minting a duplicate guest — worth calling out, since it's the one place a door sale
deliberately behaves differently from self-service guest checkout.

### 5. Sell a held seat — release the hold as part of the sale

This is the payoff of the hold workflow. Try selling one of the house seats you
held in step 1:

```
POST /event-admin/{event_id}/seating/sell
{ "seat_id": "<house-seat-1>", "tier_id": "<tier-id>", "payment_method": "free", "email": "vip@example.com" }
```

It succeeds — the box-office HELD override is deleted automatically as part of the
sale. That's the point of a hold: it's a soft "don't sell this yet," not a wall.
Now try the killed seat from step 2 the same way — **400, "This seat is blocked for
this event."** A kill is a hard no.

### 6. Reseat a guest

A guest's seat has a sightline problem, or there's a dispute at the door:

```
POST /event-admin/{event_id}/seating/reseat
{ "ticket_id": "<ticket-id>", "target_seat_id": "<free-seat-in-same-category>" }
```

The target seat must be in the **same price category** as the current seat. Frame
this as what it is — a money rule, not a missing feature. A seat's category can now
decide what its ticket cost, so moving a guest from Stalls to Balcony isn't a
courtesy shuffle; it's a change of price on a sale that already happened, and it
owes somebody a refund or an upcharge. Revel would rather make you do that
explicitly than move the guest and quietly leave the books wrong.

So: "reseat is same-zone only — a move to a different price zone is a refund plus a
new sale." Within a zone, reseat freely; that's the case that comes up at the door
ninety-nine times out of a hundred (broken seat, sightline, a family that wants to
sit together).

### 7. Check in and see the seat

Scan the ticket's QR at the door:

```
POST /event-admin/{event_id}/tickets/{code}/check-in
```

The response now includes the seat: row/number plus a human-readable sector name
("Stalls, Row C seat 12"). Ushers can read that straight off the scanner screen and
tell the guest where to go — no separate lookup, no walking them to a seating chart.

## Talking points & FAQs

**"What if two ushers try to sell the same seat at the same time?"**
Both requests lock the seat row before checking anything else, so the second one
gets a clean 409 conflict — never a double-sold seat.

**"Can the door oversell the venue?"**
No. Door sales still enforce tier and event capacity (429 sold-out / 400 "N
remaining"). What they skip are the *buyer-facing* rules — `purchasable_by`
restrictions, per-user ticket caps, and sales-window timing — because a staff member
selling at the door is making a judgment call a script shouldn't second-guess. Real
capacity is never bypassed.

**"What if I accidentally hold a seat that's already sold?"**
You can't — the override is rejected per-seat with reason `"ticketed"`, and the rest
of your batch still goes through. Nothing you do at the box office can silently
disappear a paying guest's seat.

**"Can I sell a seat I killed by mistake?"**
Release the kill first (`release_seat_ids`), then sell it. A killed seat is a hard
block by design — it should mean "genuinely not sellable" (broken, out of service),
not "temporarily held."

**"Does a hold expire?"**
No — a box-office hold (`held`) is not the same as a buyer's 10-minute checkout hold
(see [`buyer-experience.md`](buyer-experience.md)). It stays in place, per event,
until a staff member explicitly releases it or sells the seat.

**"What happens to comps in the revenue report?"**
They record 0.00 and stay that way — a comp never inflates the night's revenue number,
whatever the seat is worth. Paid door sales report at the seat's price: the tier's list
price on a flat tier, and the seat's own category price on a tier that prices by zone.

**"If we put prices up next month, does that change what tonight's door sales are
worth?"**
No. A door sale on a zone-priced tier records the amount at the time of sale, so
repricing later never rewrites tonight's takings.

**"Can I move someone to a totally different section?"**
Not with reseat — it's same-price-zone only, because a different zone can mean a
different price and that's a refund or an upcharge, not a seat move. Do it as a refund
plus a new sale so the money is right.

**"An organizer refunded a ticket from the Stripe Dashboard and Revel still shows it as
valid. Why?"**
Because **refunds have to be issued through Revel to be reflected on the ticket.** A
refund made in the Stripe Dashboard carries no reference to which ticket it was for. When
everything in the order cost the same, Revel can still work it out. When an order mixes
prices — the normal case on a zone-priced tier — it genuinely can't: a €30 refund on a
€50 + €30 order could be a partial refund on the expensive seat or a full refund on the
cheap one, and guessing wrong cancels the wrong ticket and puts a seat back on sale while
its owner is still sitting in it.

So Revel refuses to guess. Nothing is cancelled, no seat is freed, and staff with
`manage_tickets` get a notification naming the refund, the amount, and the candidate
tickets so a human can finish the job. The notification links straight to that event's
ticket list, filtered to the buyer, so the two candidates are side by side. Say it
plainly on a demo: **"issue refunds from
Revel and the ticket follows automatically; issue them from Stripe and you'll have to
cancel the ticket by hand."**

**"Do overrides work across our whole venue, or just tonight's show?"**
Per event, always. The same physical seat can be held for tonight and open for
tomorrow. Overrides never touch the venue's permanent layout — see
[`venue-and-layout.md`](venue-and-layout.md).

## Under the hood

Full journey narrative: [`https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md`](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md)
§19.7 (Box Office Seat Control), plus §6.10 and §10.5 for the surrounding event-admin
and ticket-management context.

**Endpoints** (all under `/event-admin/{event_id}/`, `manage_tickets` permission
unless noted):

- `PUT /seating/overrides` — bulk hold/kill/release. Per-seat rejection
  (`"ticketed"` / `"unknown_seat"`), never whole-batch. Release wins if a seat
  appears in both `set` and `release_seat_ids`.
- `POST /seating/sell` — door sale/comp. `payment_method` is `at_the_door` or
  `free` only. Recipient is exactly one of `email` (existing account reused, or new
  guest minted) or `user_id`. `free` always records `price_paid = 0.00`;
  `at_the_door` stamps the seat's resolved zone price on a tier with a zone price map,
  and leaves `price_paid` null on a flat tier (where the tier price reconstructs it
  exactly). A seat in a zone the tier's map doesn't price is a 400 — never a fallback to
  `tier.price`.
- `POST /seating/reseat` — move a PENDING/ACTIVE ticket to a free seat in the same
  `default_price_category`. Same-category is a money-correctness rule: the target
  seat must cost what the guest already paid.
- `POST /tickets/{code}/check-in` — **`check_in_attendees` permission**, not
  `manage_tickets`. Response (`CheckInResponseSchema`) includes `seat` and
  `sector_name`.

Service code, if you want to verify behavior directly: `events/service/seating/overrides.py`
(bulk overrides, seat/ticket locking), `events/service/seating/box_office.py` (`sell`,
`reseat`, recipient resolution), and `events/controllers/event_admin/seating.py` /
`events/controllers/event_admin/tickets.py` for the controller wiring.
