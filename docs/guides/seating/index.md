# Reserved Seating

Revel's reserved-seating system lets any venue — from a 120-seat comedy club to a
2,500-seat theatre — sell assigned seats with a familiar, box-office-grade workflow:
a visual layout with price categories, a live seat map for buyers, adjacency-aware
"best available" assignment, and a real box office for event night (holds, kills,
door sales, comps, and reseats).

These guides are written for **sales reps**: understand the feature, pitch it
honestly, and demo it. Each page has a "Demo it" section with a concrete step path,
and a "Talking points & FAQs" section with the questions prospects actually ask —
including the honest limits, so you never overpromise.

## The four guides

| Guide | What it covers | Best for pitching to |
|-------|----------------|----------------------|
| [Venue & Layout Setup](venue-and-layout.md) | Venues, seated/standing sectors, the grid editor, natural row ordering, price categories, seat painting | The organizer who sets things up once and reuses it |
| [Tickets, Pricing & Seat Assignment](tiers-and-pricing.md) | The pricing model (zones on the map, prices on the event), per-seat pricing, the three assignment modes, how "best available" picks seats | Anyone who asks "can the price depend on the seat?", "can I do student/senior prices?" or "can it seat a family together?" |
| [The Buyer Experience](buyer-experience.md) | The live availability map, seat holds, checkout, guest checkout, accessible seating | The prospect worried about the buyer's experience and double-sells |
| [Box Office & Event-Day Operations](box-office.md) | Holds/kills, door sales, comps, reseats, seat-on-check-in | The actual box-office manager — the person who decides |

## The five-minute pitch

If you have one prospect and five minutes, tell this story:

1. **"Set up your room once."** The organizer draws sectors and seats, and Revel
   figures out the front-to-back, left-to-right order automatically — no manual seat
   numbering. Paint price zones with colors (Stalls, Balcony, Boxes). See
   [Venue & Layout Setup](venue-and-layout.md).
2. **"Price it however you sell it."** Paint zones on the venue, price zones on the
   event's ticket tier — one mechanism, so the same seats can be premium on Saturday and
   half-price midweek. Sell the classic theatre way — **the buyer picks a seat and pays
   what that seat costs**, front stalls €80, balcony €30, all on one map and one checkout.
   And because multiple ticket types can share one price zone, you also get **adult /
   student / child prices on the same seats** out of the box. Need a zone to have its own
   cap or its own on-sale date? Give that zone its own tier. See
   [Tickets & Pricing](tiers-and-pricing.md).
3. **"Buyers get a real seat map."** They see what's open live, tap to hold seats (so
   nobody grabs them mid-checkout), and buy. Or they pick a zone, hit **"best available"**,
   and Revel seats their whole party together in that zone, up front and center — and
   never quietly hands a wheelchair space to someone who didn't ask for it. See
   [The Buyer Experience](buyer-experience.md).
4. **"And on the night, it's a box office."** Hold the house seats for the crew, kill
   the broken one, sell cash at the door, comp the press, move a guest who complains —
   and when you scan a ticket you see the seat, so ushers can point. See
   [Box Office](box-office.md).
5. **"No double-sells, ever."** Two people can never buy the same seat — the platform
   guarantees it, and we load-tested it with 50 people fighting over 10 seats. Zero
   errors, zero double-sells.

## Which prospect are you talking to?

**Small & scrappy — the comedy club, the 200-seat black box, the jazz bar.**
They care about: fast setup, cash at the door, comps for friends-of-the-house, and not
losing the plot on a busy Friday. Lead with the **grid editor** (rooms are small, setup
is minutes), **door sales & comps** in the box-office guide, and the fact that there's
**no per-seat cost or minimum** — a standing-room-only night is just a GA sector with a
capacity. The Chuckle Cellar (80 seats + 40 standing) in the demo data is your prop.

**Big & formal — the theatre, the opera house, the concert hall.**
They care about: price zones, subscriptions-adjacent pricing, accessible seating done
right, holds for production/press, and reseating. Lead with **price categories +
concession pricing** (tiers guide), **best-available with accessibility protection**
(buyer guide), and the **hold/kill/reseat** box-office toolkit. Teatro Grande (1,352
seats across Platea, Galleria, and four Palco boxes, with painted price zones) is your
prop. Also lead with **per-seat prices** — pick your seat, pay what that seat costs — which
is the model they already run today. Be honest about the current limits: reseating is
**same-price-zone only** (a move across zones is a refund plus a new sale, because it's a
change of price); repainting the map is **venue-wide and immediate**, so it moves prices at
every event in that building at once; and there's **no season-subscription /
fixed-subscriber-seat** feature yet.

## Run a full demo (end to end)

To show the whole loop on the seeded demo data:

1. **Setup** ([Venue & Layout Setup](venue-and-layout.md#demo-it)) — open Teatro
   Grande, show the sectors, the painted Galleria/Platea/Palco categories, and add or
   repaint a seat to prove the round-trip (paint persists and shows on reload).
2. **Configure a tier** ([Tickets & Pricing](tiers-and-pricing.md#demo-it)) — create a
   `best_available` tier on the Galleria sector and a `user_choice` tier on a stalls
   sector; give each a price per zone, then try to misconfigure one (e.g. a user-choice
   price map with a painted zone left out, or a best-available hold that names no zone) to
   show the guardrails.
3. **Buy** ([The Buyer Experience](buyer-experience.md#demo-it)) — pull the availability
   map, hold two adjacent seats, and check out; then do a **best-available** buy and
   show that the seats you were shown are exactly what you get.
4. **Run the night** ([Box Office](box-office.md#demo-it)) — hold a block for "house",
   kill one seat, sell a cash ticket at the door, comp one, then scan a ticket and show
   the seat in the result.

## The canonical reference

These guides are the narrative layer. The step-by-step, endpoint-level flows live in
the platform's [User Journeys](https://github.com/letsrevel/revel-backend/blob/main/USER_JOURNEYS.md) document, **Journey 19 —
Venue & Seating** (each guide links to the relevant sub-sections). When a detail here
and the journeys ever disagree, the journeys and the code win.
