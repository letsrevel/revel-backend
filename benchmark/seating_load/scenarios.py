"""The four seating load-test scenarios plus the post-run ORM invariant sweep.

All load goes over HTTP; the ORM is only used for fixture selection and
post-run verification. Seeds are fixed so seat/user selection is deterministic.
"""

import dataclasses
import random
import threading
import time
import typing as t
import uuid
from concurrent.futures import ThreadPoolExecutor

from .harness import (
    Call,
    LoadClient,
    ScenarioResult,
    check,
    mint_tokens,
    percentile,
    pick_users,
    print_latency_block,
    status_counts,
)

SYMPHONY_EVENT = "Symphony No. 9 — New Year Gala"
CHUCKLE_EVENT = "Headliner Night: Late Show"

# Disjoint user slices so per-user throttles (100/min) never accumulate across scenarios.
USERS_STORM = (0, 50)
USERS_HERD_A = (50, 100)
USERS_HERD_B = (150, 30)
USERS_POLLERS = (180, 30)
USERS_MUTATORS = (210, 8)
USERS_BUYERS = (218, 30)
USERS_ATTACKERS = (400, 10)  # past the buyer scan window (218..338)
USERS_OBSERVER = (420, 1)

# Disjoint Platea seat ranges (sorted by row_label, adjacency_index, id).
SEATS_CONTESTED = (0, 10)
SEATS_MUTATOR_POOL = (700, 40)


@dataclasses.dataclass
class Fixtures:
    """Resolved seed objects the scenarios run against."""

    symphony_event_id: uuid.UUID
    platea_tier_id: uuid.UUID
    galleria_tier_id: uuid.UUID
    chuckle_event_id: uuid.UUID
    riser_tier_id: uuid.UUID
    platea_seat_ids: list[uuid.UUID]
    galleria_seat_count: int
    riser_seat_count: int


def prepare_fixtures() -> Fixtures:
    """Resolve fixtures and apply the two documented seed mutations (idempotent).

    Mutations, all printed loudly:
    1. Flip the Symphony "Platea" tier to a FREE tier (scenario 4 needs a free
       user_choice tier on Teatro Grande; the seed only ships offline ones).
    2. Raise ``max_tickets_per_user`` 1 -> 4 on the two target events: the hold cap
       (``acquire_seats``) is ``event.max_tickets_per_user or 10``, so the seeded cap
       of 1 would 409 every multi-seat hold and every best-available party before any
       contention is exercised.
    3. WORKAROUND for a real bug this harness surfaced: seeded sector shapes are
       ``[[x, y], ...]`` (the VenueSector.shape documented format) but the response
       schemas require ``[{"x": .., "y": ..}, ...]`` (Coordinate2D), which 500s the
       chart endpoint and checkout serialization. Normalize shapes so load numbers
       are measurable. The schema/model mismatch still needs a product fix.
    """
    from events.models import Event, TicketTier, VenueSeat, VenueSector

    symphony = Event.objects.get(name=SYMPHONY_EVENT)
    chuckle = Event.objects.get(name=CHUCKLE_EVENT)
    platea = TicketTier.objects.get(event=symphony, name="Platea")
    galleria = TicketTier.objects.get(event=symphony, name="Galleria")
    riser = TicketTier.objects.get(event=chuckle, name="Riser")

    if platea.payment_method != TicketTier.PaymentMethod.FREE:
        platea.payment_method = TicketTier.PaymentMethod.FREE
        platea.price = 0
        platea.save(update_fields=["payment_method", "price"])
        print("  [prep] Flipped Symphony 'Platea' tier to payment_method=free, price=0")

    for event in (symphony, chuckle):
        if event.max_tickets_per_user != 4:
            event.max_tickets_per_user = 4
            event.save(update_fields=["max_tickets_per_user"])
            print(f"  [prep] Raised max_tickets_per_user 1 -> 4 on '{event.name}' (hold cap gates multi-seat holds)")

    fixed = 0
    for sector in VenueSector.objects.exclude(shape=None):
        if sector.shape and isinstance(sector.shape[0], list):
            sector.shape = [{"x": p[0], "y": p[1]} for p in sector.shape]
            sector.save(update_fields=["shape"])
            fixed += 1
    if fixed:
        print(f"  [prep] WORKAROUND: normalized {fixed} sector shapes [[x,y],...] -> Coordinate2D dicts")

    platea_seats = list(
        VenueSeat.objects.filter(sector_id=platea.sector_id, is_active=True)
        .order_by("row_label", "adjacency_index", "id")
        .values_list("id", flat=True)
    )
    galleria_count = VenueSeat.objects.filter(sector_id=galleria.sector_id, is_active=True).count()
    riser_count = VenueSeat.objects.filter(sector_id=riser.sector_id, is_active=True).count()
    return Fixtures(
        symphony_event_id=symphony.id,
        platea_tier_id=platea.id,
        galleria_tier_id=galleria.id,
        chuckle_event_id=chuckle.id,
        riser_tier_id=riser.id,
        platea_seat_ids=platea_seats,
        galleria_seat_count=galleria_count,
        riser_seat_count=riser_count,
    )


def _seat_slice(fx: Fixtures, span: tuple[int, int]) -> list[uuid.UUID]:
    offset, count = span
    return fx.platea_seat_ids[offset : offset + count]


def _tokens_for(span: tuple[int, int]) -> list[str]:
    offset, count = span
    return mint_tokens(pick_users(offset, count))


def _hold_path(event_id: uuid.UUID) -> str:
    return f"/api/events/{event_id}/seating/holds"


def _run_concurrently(tasks: list[t.Callable[[], None]], stagger_s: float = 0.0, use_barrier: bool = True) -> float:
    """Run tasks on one thread each; barrier-synced or stagger-started. Returns wall seconds."""
    barrier = threading.Barrier(len(tasks)) if use_barrier else None

    def wrap(fn: t.Callable[[], None]) -> None:
        if barrier is not None:
            barrier.wait()
        fn()

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
        futures = []
        for i, fn in enumerate(tasks):
            if stagger_s and i:
                time.sleep(stagger_s)
            futures.append(pool.submit(wrap, fn))
        for fut in futures:
            fut.result()
    return time.perf_counter() - start


# --------------------------------------------------------------------------- #
# Scenario 1: hold storm                                                      #
# --------------------------------------------------------------------------- #


def scenario_hold_storm(client: LoadClient, fx: Fixtures, seed: int) -> ScenarioResult:
    """50 users race all-or-nothing holds for the same 10 Platea seats."""
    print("\n=== Scenario 1: hold storm (50 users vs 10 Teatro Platea seats) ===")
    notes: list[str] = []
    contested = _seat_slice(fx, SEATS_CONTESTED)
    tokens = _tokens_for(USERS_STORM)
    observer_token = _tokens_for(USERS_OBSERVER)[0]
    path = _hold_path(fx.symphony_event_id)

    requests_per_user = [random.Random(seed + i).sample(contested, 2) for i in range(len(tokens))]
    results: list[tuple[int, Call]] = []
    lock = threading.Lock()

    def make_task(i: int) -> t.Callable[[], None]:
        def task() -> None:
            body = {"seat_ids": [str(s) for s in requests_per_user[i]]}
            call = client.request("POST", path, tokens[i], body, label="hold")
            with lock:
                results.append((i, call))

        return task

    wall = _run_concurrently([make_task(i) for i in range(len(tokens))])
    calls = client.take_calls()
    print_latency_block("hold storm", calls, wall)

    counts = status_counts(calls)
    ok = check(counts["5xx"] == 0 and counts["transport"] == 0, "zero 5xx", f"5xx/transport: {counts}", notes)

    # Response-level: for each contested seat at most one 200 claims it.
    claims: dict[str, int] = {}
    for _, call in results:
        if call.status == 200 and isinstance(call.body, dict):
            for sid in call.body.get("held_seat_ids", []):
                claims[sid] = claims.get(sid, 0) + 1
    over = {s: n for s, n in claims.items() if n > 1}
    ok &= check(not over, "at most one winner per seat (responses)", f"multi-claimed seats: {over}", notes)

    losers_clean = all(
        isinstance(call.body, dict) and call.body.get("conflicts") for _, call in results if call.status == 409
    )
    ok &= check(losers_clean, "all 409s carry non-empty conflict bodies", "409 without conflicts body", notes)

    # ORM ground truth: <=1 active hold per contested seat, owned by a responder.
    from events.models import SeatHold

    holds = list(SeatHold.objects.active().filter(event_id=fx.symphony_event_id, seat_id__in=contested))
    per_seat: dict[uuid.UUID, int] = {}
    for h in holds:
        per_seat[h.seat_id] = per_seat.get(h.seat_id, 0) + 1
    ok &= check(
        all(n <= 1 for n in per_seat.values()),
        f"ORM: {len(holds)} active holds, one per seat",
        f"ORM: duplicate holds per seat: {per_seat}",
        notes,
    )

    avail = client.request(
        "GET", f"/api/events/{fx.symphony_event_id}/seating/availability", observer_token, label="avail"
    )
    held_visible = (
        isinstance(avail.body, dict)
        and avail.status == 200
        and all(avail.body["seats"].get(str(h.seat_id)) == "held" for h in holds)
    )
    ok &= check(held_visible, "availability shows every winner's seat as 'held'", "availability mismatch", notes)
    client.take_calls()

    notes.insert(0, f"statuses={counts} wall={wall:.2f}s p95={percentile([c.elapsed_ms for c in calls], 95):.0f}ms")
    return ScenarioResult("hold_storm", ok, notes)


# --------------------------------------------------------------------------- #
# Scenario 2: best-available herd                                             #
# --------------------------------------------------------------------------- #


def _herd_wave(
    client: LoadClient,
    event_id: uuid.UUID,
    tier_id: uuid.UUID,
    tokens: list[str],
    seed: int,
    wave: str,
    notes: list[str],
) -> bool:
    """One best-available wave: staggered start, concurrent execution, overlap checks."""
    path = f"/api/events/{event_id}/seating/holds/best-available"
    sizes = [random.Random(seed + i).randint(2, 4) for i in range(len(tokens))]

    # Each user releases their own leftover holds first, otherwise a re-run measures
    # the per-user hold cap instead of adjacency contention. Not counted in the stats.
    with ThreadPoolExecutor(max_workers=20) as pool:
        for token in tokens:
            pool.submit(client.request, "DELETE", _hold_path(event_id), token, None, "prep_release")
    client.take_calls()

    def make_task(i: int) -> t.Callable[[], None]:
        def task() -> None:
            body = {"tier_id": str(tier_id), "quantity": sizes[i]}
            client.request("POST", path, tokens[i], body, label=f"best_available_{wave}")

        return task

    wall = _run_concurrently([make_task(i) for i in range(len(tokens))], stagger_s=0.015, use_barrier=False)
    calls = client.take_calls()
    print_latency_block(f"best-available {wave}", calls, wall)

    counts = status_counts(calls)
    ok = check(
        counts["5xx"] == 0 and counts["transport"] == 0,
        f"{wave}: zero 5xx (savepoint-retry survived contention)",
        f"{wave}: 5xx/transport: {counts}",
        notes,
    )

    assigned: list[str] = []
    fulfilled = 0
    for c in calls:
        if c.status == 200 and isinstance(c.body, dict):
            held = c.body.get("held_seat_ids", [])
            assigned.extend(held)
            fulfilled += 1
    dupes = len(assigned) - len(set(assigned))
    ok &= check(
        dupes == 0,
        f"{wave}: no overlapping seat assignments ({len(assigned)} seats)",
        f"{wave}: {dupes} overlapping seats",
        notes,
    )
    print(f"    parties fulfilled: {fulfilled}/{len(tokens)} (seats granted: {len(assigned)})")
    notes.append(
        f"{wave}: {fulfilled}/{len(tokens)} parties fulfilled, {counts['409']}x409, "
        f"p95={percentile([c.elapsed_ms for c in calls], 95):.0f}ms"
    )
    return ok


def scenario_best_available_herd(client: LoadClient, fx: Fixtures, seed: int) -> ScenarioResult:
    """100 parties on Teatro Galleria, then 30 parties on the 47-seat Chuckle Riser (exhaustion)."""
    print("\n=== Scenario 2: best-available herd ===")
    print(f"  wave A: 100 parties (2-4) on Teatro Galleria ({fx.galleria_seat_count} seats)")
    print(f"  wave B: 30 parties (2-4) on Chuckle Cellar Riser ({fx.riser_seat_count} seats) — forces exhaustion")
    notes: list[str] = []
    ok = _herd_wave(client, fx.symphony_event_id, fx.galleria_tier_id, _tokens_for(USERS_HERD_A), seed, "A", notes)
    ok &= _herd_wave(client, fx.chuckle_event_id, fx.riser_tier_id, _tokens_for(USERS_HERD_B), seed + 1000, "B", notes)
    return ScenarioResult("best_available_herd", ok, notes)


# --------------------------------------------------------------------------- #
# Scenario 3: availability polling under mutation                             #
# --------------------------------------------------------------------------- #


def scenario_availability_polling(client: LoadClient, fx: Fixtures, seed: int) -> ScenarioResult:
    """30 pollers x (20 availability + 1 chart) while mutators churn holds."""
    print("\n=== Scenario 3: availability polling (30 pollers x 20 iters + 1 chart each) ===")
    notes: list[str] = []
    poll_tokens = _tokens_for(USERS_POLLERS)
    mutator_tokens = _tokens_for(USERS_MUTATORS)
    pool = _seat_slice(fx, SEATS_MUTATOR_POOL)
    stop = threading.Event()
    avail_path = f"/api/events/{fx.symphony_event_id}/seating/availability"
    chart_path = f"/api/events/{fx.symphony_event_id}/seating/chart"

    def mutator(idx: int) -> None:
        rng = random.Random(seed + 9000 + idx)
        while not stop.is_set():
            seats = [str(s) for s in rng.sample(pool, 2)]
            client.request(
                "POST", _hold_path(fx.symphony_event_id), mutator_tokens[idx], {"seat_ids": seats}, label="mut_hold"
            )
            client.request(
                "DELETE",
                _hold_path(fx.symphony_event_id),
                mutator_tokens[idx],
                {"seat_ids": seats},
                label="mut_release",
            )
            stop.wait(1.5)  # pace: ~80 writes/min/user, under the 100/min WriteThrottle

    def poller(idx: int) -> None:
        for iteration in range(20):
            client.request("GET", avail_path, poll_tokens[idx], label="availability")
            if iteration == 10:
                client.request("GET", chart_path, poll_tokens[idx], label="chart")
            time.sleep(0.1)

    mut_pool = ThreadPoolExecutor(max_workers=len(mutator_tokens))
    mut_futures = [mut_pool.submit(mutator, i) for i in range(len(mutator_tokens))]
    wall = _run_concurrently([lambda i=i: poller(i) for i in range(len(poll_tokens))])  # type: ignore[misc]
    stop.set()
    for fut in mut_futures:
        fut.result()
    mut_pool.shutdown()

    calls = client.take_calls()
    avail_calls = [c for c in calls if c.label == "availability"]
    chart_calls = [c for c in calls if c.label == "chart"]
    mut_calls = [c for c in calls if c.label.startswith("mut_")]
    print_latency_block("availability", avail_calls, wall)
    print_latency_block("chart", chart_calls, wall)
    print_latency_block("mutators", mut_calls, wall)

    counts = status_counts(calls)
    ok = check(counts["5xx"] == 0 and counts["transport"] == 0, "zero 5xx", f"5xx/transport: {counts}", notes)
    avail_p95 = percentile([c.elapsed_ms for c in avail_calls], 95)
    chart_p95 = percentile([c.elapsed_ms for c in chart_calls], 95)
    # Soft targets: report actuals either way.
    check(
        avail_p95 < 500,
        f"availability p95 {avail_p95:.0f}ms < 500ms (soft)",
        f"availability p95 {avail_p95:.0f}ms >= 500ms (soft target missed)",
        notes,
    )
    check(
        chart_p95 < 1000,
        f"chart p95 {chart_p95:.0f}ms < 1000ms (soft)",
        f"chart p95 {chart_p95:.0f}ms >= 1000ms (soft target missed)",
        notes,
    )
    notes.insert(0, f"avail p95={avail_p95:.0f}ms chart p95={chart_p95:.0f}ms statuses={counts}")
    return ScenarioResult("availability_polling", ok, notes)


# --------------------------------------------------------------------------- #
# Scenario 4: purchase race                                                   #
# --------------------------------------------------------------------------- #


def _eligible_users(event_id: uuid.UUID, offset: int, want: int) -> list[t.Any]:
    """First ``want`` users from ``offset`` that pass the event's eligibility gates.

    Seeded users can trip org gates (blacklist fuzzy match -> request_whitelist), which
    correctly 400s at checkout — those are filtered out so the race measures seating,
    not org-vetting behaviour. Users without allowance for 2 more tickets under the
    event's per-user cap are also skipped (keeps repeat runs meaningful).
    """
    from events.models import Event, Ticket
    from events.service.event_manager import EventManager

    event = Event.objects.get(pk=event_id)
    cap = event.max_tickets_per_user
    out: list[t.Any] = []
    skipped = 0
    for user in pick_users(offset, want * 4):
        owned = Ticket.objects.filter(event=event, user=user).exclude(status=Ticket.TicketStatus.CANCELLED).count()
        allowance_ok = cap is None or owned <= cap - 2
        if allowance_ok and EventManager(user, event).check_eligibility().allowed:
            out.append(user)
            if len(out) == want:
                break
        else:
            skipped += 1
    if len(out) < want:
        raise RuntimeError(f"Only {len(out)}/{want} eligible users from offset {offset}")
    if skipped:
        print(f"  [setup] skipped {skipped} seeded user(s) failing eligibility gates (correct 400s, not seating)")
    return out


def _free_platea_seat_pairs(fx: Fixtures, pairs: int) -> list[list[uuid.UUID]]:
    """First ``pairs`` adjacent pairs of unsold/unheld/unblocked Platea seats.

    Dynamic (instead of a fixed range) so purchase-race re-runs keep working after
    earlier runs ticketed seats. Skips the contested and mutator ranges used by
    scenarios 1 and 3.
    """
    from events.models import EventSeatOverride, SeatHold, Ticket

    reserved = set(_seat_slice(fx, SEATS_CONTESTED)) | set(_seat_slice(fx, SEATS_MUTATOR_POOL))
    taken = set(
        Ticket.objects.filter(event_id=fx.symphony_event_id, seat_id__in=fx.platea_seat_ids)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .values_list("seat_id", flat=True)
    )
    taken |= set(
        SeatHold.objects.active()
        .filter(event_id=fx.symphony_event_id, seat_id__in=fx.platea_seat_ids)
        .values_list("seat_id", flat=True)
    )
    taken |= set(EventSeatOverride.objects.filter(event_id=fx.symphony_event_id).values_list("seat_id", flat=True))
    free = [s for s in fx.platea_seat_ids if s not in taken and s not in reserved]
    if len(free) < pairs * 2:
        raise RuntimeError(f"Need {pairs * 2} free Platea seats, found {len(free)}")
    return [free[2 * i : 2 * i + 2] for i in range(pairs)]


def scenario_purchase_race(client: LoadClient, fx: Fixtures, seed: int) -> ScenarioResult:
    """30 buyers hold then buy two free Platea seats each; 10 attackers race the same seats."""
    print("\n=== Scenario 4: purchase race (30 buyers x 2 seats + 10 attackers, free Platea tier) ===")
    notes: list[str] = []
    buyer_users = _eligible_users(fx.symphony_event_id, USERS_BUYERS[0], USERS_BUYERS[1])
    buyer_tokens = mint_tokens(buyer_users)
    attacker_tokens = _tokens_for(USERS_ATTACKERS)
    seats_of = _free_platea_seat_pairs(fx, len(buyer_tokens))
    checkout_path = f"/api/events/{fx.symphony_event_id}/tickets/{fx.platea_tier_id}/checkout"

    # Phase 1: each buyer holds two distinct seats (concurrent, no contention expected).
    def hold_task(i: int) -> t.Callable[[], None]:
        def task() -> None:
            body = {"seat_ids": [str(s) for s in seats_of[i]]}
            client.request("POST", _hold_path(fx.symphony_event_id), buyer_tokens[i], body, label="hold")

        return task

    wall = _run_concurrently([hold_task(i) for i in range(len(buyer_tokens))])
    hold_calls = client.take_calls()
    print_latency_block("buyer holds", hold_calls, wall)
    ok = check(
        all(c.status == 200 for c in hold_calls), "all 30 buyer holds acquired", "some buyer holds failed", notes
    )

    # Phase 2: buyers check out their held seat while attackers race the same seats.
    rng = random.Random(seed)
    victim_of = [rng.randrange(len(buyer_tokens)) for _ in range(len(attacker_tokens))]

    def buy_task(i: int) -> t.Callable[[], None]:
        def task() -> None:
            tickets = [{"guest_name": f"Load Buyer {i}-{n}", "seat_id": str(s)} for n, s in enumerate(seats_of[i])]
            client.request("POST", checkout_path, buyer_tokens[i], {"tickets": tickets}, label="buy")

        return task

    def attack_task(a: int) -> t.Callable[[], None]:
        def task() -> None:
            victim_seats = seats_of[victim_of[a]]
            tickets = [{"guest_name": f"Seat Sniper {a}-{n}", "seat_id": str(s)} for n, s in enumerate(victim_seats)]
            client.request("POST", checkout_path, attacker_tokens[a], {"tickets": tickets}, label="attack")

        return task

    tasks = [buy_task(i) for i in range(len(buyer_tokens))] + [attack_task(a) for a in range(len(attacker_tokens))]
    wall = _run_concurrently(tasks)
    calls = client.take_calls()
    buy_calls = [c for c in calls if c.label == "buy"]
    attack_calls = [c for c in calls if c.label == "attack"]
    print_latency_block("buyer checkout", buy_calls, wall)
    print_latency_block("attacker checkout", attack_calls, wall)

    counts = status_counts(calls)
    ok &= check(counts["5xx"] == 0 and counts["transport"] == 0, "zero 5xx", f"5xx/transport: {counts}", notes)
    buys_ok = all(
        c.status == 200 and isinstance(c.body, dict) and not c.body.get("requires_payment") for c in buy_calls
    )
    ok &= check(
        buys_ok, "all 30 buyers got 200 with tickets", f"buyer statuses: {sorted(c.status for c in buy_calls)}", notes
    )
    attacks_ok = all(c.status in (400, 409) for c in attack_calls)
    ok &= check(
        attacks_ok,
        f"all attackers cleanly rejected ({sorted(c.status for c in attack_calls)})",
        f"attacker statuses: {sorted(c.status for c in attack_calls)}",
        notes,
    )

    # ORM ground truth: each buyer owns exactly their held seat; no seat double-sold.
    from django.db.models import Count

    from events.models import Ticket

    all_buyer_seats = [seat for pair in seats_of for seat in pair]
    tickets = Ticket.objects.filter(event_id=fx.symphony_event_id, seat_id__in=all_buyer_seats).exclude(
        status=Ticket.TicketStatus.CANCELLED
    )
    by_seat = {t_.seat_id: t_.user_id for t_ in tickets}
    exact = all(by_seat.get(seat) == buyer_users[i].id for i in range(len(buyer_users)) for seat in seats_of[i])
    ok &= check(exact, "ORM: every ticket sits on exactly the buyer's held seat", "ORM: seat/owner mismatch", notes)
    dup = (
        Ticket.objects.filter(event_id=fx.symphony_event_id)
        .exclude(status=Ticket.TicketStatus.CANCELLED)
        .exclude(seat=None)
        .values("seat_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .count()
    )
    ok &= check(dup == 0, "ORM: no seat has >1 non-cancelled ticket", f"ORM: {dup} double-sold seats", notes)
    attack_statuses = sorted({c.status for c in attack_calls})
    notes.insert(
        0, f"buyers 200={sum(1 for c in buy_calls if c.status == 200)}/30, attacker statuses={attack_statuses}"
    )
    return ScenarioResult("purchase_race", ok, notes)


# --------------------------------------------------------------------------- #
# Global invariant sweep                                                      #
# --------------------------------------------------------------------------- #


def invariant_sweep() -> ScenarioResult:
    """Post-run ORM sweep: duplicate tickets, hold owner XOR, hold lifetime bound."""
    print("\n=== Global invariant sweep (ORM) ===")
    import datetime

    from django.db.models import Count, F, Q

    from events.models import SeatHold, Ticket

    notes: list[str] = []
    dup = (
        Ticket.objects.exclude(status=Ticket.TicketStatus.CANCELLED)
        .exclude(seat=None)
        .values("event_id", "seat_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .count()
    )
    ok = check(
        dup == 0, "no (event, seat) with >1 non-cancelled ticket", f"{dup} double-sold (event, seat) pairs", notes
    )

    xor_bad = SeatHold.objects.filter(
        (Q(user__isnull=False) & ~Q(guest_session="")) | (Q(user__isnull=True) & Q(guest_session=""))
    ).count()
    ok &= check(
        xor_bad == 0, "all SeatHold rows satisfy the XOR owner constraint", f"{xor_bad} rows violate owner XOR", notes
    )

    over_ttl = SeatHold.objects.filter(expires_at__gt=F("acquired_at") + datetime.timedelta(minutes=30)).count()
    ok &= check(
        over_ttl == 0,
        "no hold expires more than 30min after acquisition",
        f"{over_ttl} holds exceed the 30min lifetime cap",
        notes,
    )
    return ScenarioResult("invariant_sweep", ok, notes)
