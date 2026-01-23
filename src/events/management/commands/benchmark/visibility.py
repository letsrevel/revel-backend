"""Visibility flags benchmark.

Benchmarks the build_attendee_visibility_flags task which previously had
a CRITICAL N+1 issue: resolve_visibility() did 5 queries per viewer×target pair.

For an event with 50 attendees and 100 viewers, this resulted in:
50 × 100 × 5 = 25,000 queries!

After optimization: 4 queries total (for context) + O(1) per pair.
"""

import gc
import time
import typing as t

from django.db import connection, reset_queries, transaction

from accounts.models import RevelUser
from events.models import (
    EventInvitation,
    EventRSVP,
    GeneralUserPreferences,
    OrganizationMember,
    Ticket,
)
from events.service.user_preferences_service import VisibilityContext, resolve_visibility_fast

from .base import BaseBenchmarkCommand, BenchmarkResult, BenchmarkScenario


class VisibilityBenchmark(BaseBenchmarkCommand):
    """Benchmark visibility flag building to identify N+1 issues."""

    help = "Benchmark visibility flag building (P0 N+1 issue)"
    benchmark_name = "Visibility Flags"

    def add_extra_arguments(self, parser: t.Any) -> None:
        """Add visibility-specific arguments."""
        parser.add_argument(
            "--viewers",
            type=int,
            default=20,
            help="Number of viewers (users who can see attendee list)",
        )
        parser.add_argument(
            "--attendees",
            type=int,
            default=10,
            help="Number of attendees (targets to check visibility for)",
        )
        parser.add_argument(
            "--scenario",
            type=str,
            choices=["all", "small", "medium", "large"],
            default="all",
            help="Which scenarios to run",
        )

    def create_scenarios(self, options: dict[str, t.Any]) -> list[BenchmarkScenario]:
        """Create visibility benchmark scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Setting up benchmark scenarios ---"))
        scenarios: list[BenchmarkScenario] = []
        scenario_filter = options.get("scenario", "all")

        # Custom scenario based on --viewers and --attendees
        viewers = options.get("viewers", 20)
        attendees = options.get("attendees", 10)

        if scenario_filter == "all":
            # Small scenario: 10 viewers, 5 attendees = 50 pairs = 250 queries worst case
            scenarios.append(self._create_visibility_scenario("small", viewers=10, attendees=5))

            # Medium scenario: 50 viewers, 20 attendees = 1000 pairs = 5000 queries worst case
            scenarios.append(self._create_visibility_scenario("medium", viewers=50, attendees=20))

            # Large scenario: 100 viewers, 50 attendees = 5000 pairs = 25000 queries worst case
            scenarios.append(self._create_visibility_scenario("large", viewers=100, attendees=50))
        elif scenario_filter == "small":
            scenarios.append(self._create_visibility_scenario("small", viewers=10, attendees=5))
        elif scenario_filter == "medium":
            scenarios.append(self._create_visibility_scenario("medium", viewers=50, attendees=20))
        elif scenario_filter == "large":
            scenarios.append(self._create_visibility_scenario("large", viewers=100, attendees=50))
        else:
            # Custom based on --viewers and --attendees
            scenarios.append(self._create_visibility_scenario("custom", viewers=viewers, attendees=attendees))

        return scenarios

    def run_benchmarks(self, scenarios: list[BenchmarkScenario], runs: int) -> dict[str, list[BenchmarkResult]]:
        """Run visibility benchmarks for all scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Running Benchmarks ---"))
        results: dict[str, list[BenchmarkResult]] = {}

        for scenario in scenarios:
            self.stdout.write(f"\n  Scenario: {scenario.name}")
            self.stdout.write(f"  Description: {scenario.description}")

            scenario_results: list[BenchmarkResult] = []

            # Benchmark 1: resolve_visibility() per pair (OLD N+1 behavior)
            result = self._benchmark_resolve_visibility_n1(scenario, runs)
            scenario_results.append(result)

            # Benchmark 2: resolve_visibility_fast() with context (OPTIMIZED)
            result = self._benchmark_resolve_visibility_optimized(scenario, runs)
            scenario_results.append(result)

            # Benchmark 3: Full build_attendee_visibility_flags task (uses optimized path)
            result = self._benchmark_full_visibility_task(scenario, runs)
            scenario_results.append(result)

            results[scenario.name] = scenario_results

        return results

    def _create_visibility_scenario(
        self,
        name: str,
        *,
        viewers: int,
        attendees: int,
    ) -> BenchmarkScenario:
        """Create a visibility scenario with specified viewer/attendee counts."""
        self.stdout.write(f"  Creating {name.upper()} scenario ({viewers} viewers, {attendees} attendees)...")

        owner = self.create_test_user(f"vis_{name}_owner")
        org = self.create_test_organization(f"vis_{name}", owner)

        # Create staff
        population = self.populate_organization(org, staff_count=3)

        event = self.create_test_event(org, f"Visibility {name.title()} Event")
        tier = self.create_test_tier(event)

        # Create attendees with varied visibility preferences
        attendee_users: list[RevelUser] = []
        for i in range(attendees):
            user = self.create_test_user(f"vis_{name}_attendee_{i}")

            # Vary visibility preferences (may already exist from signal)
            prefs, _ = GeneralUserPreferences.objects.get_or_create(user=user)
            if i % 4 == 0:
                prefs.show_me_on_attendee_list = GeneralUserPreferences.VisibilityPreference.ALWAYS
            elif i % 4 == 1:
                prefs.show_me_on_attendee_list = GeneralUserPreferences.VisibilityPreference.TO_MEMBERS
            elif i % 4 == 2:
                prefs.show_me_on_attendee_list = GeneralUserPreferences.VisibilityPreference.TO_INVITEES
            else:
                prefs.show_me_on_attendee_list = GeneralUserPreferences.VisibilityPreference.TO_BOTH
            prefs.save()

            # Make some attendees members
            if i % 3 == 0:
                OrganizationMember.objects.create(
                    organization=org,
                    user=user,
                    status=OrganizationMember.MembershipStatus.ACTIVE,
                )

            # Create tickets for attendees
            Ticket.objects.create(
                event=event,
                tier=tier,
                user=user,
                guest_name=user.get_display_name(),
                status=Ticket.TicketStatus.ACTIVE,
            )

            attendee_users.append(user)

        # Create viewers (some overlap with attendees, some are just invitees)
        viewer_users: list[RevelUser] = []

        # Include staff and owner as viewers
        viewer_users.append(owner)
        viewer_users.extend(population["staff"])

        # Include some attendees as viewers
        viewer_users.extend(attendee_users[: min(len(attendee_users), viewers // 2)])

        # Create additional invitees (not attendees)
        remaining_viewers = viewers - len(viewer_users)
        for i in range(max(0, remaining_viewers)):
            user = self.create_test_user(f"vis_{name}_invitee_{i}")
            EventInvitation.objects.create(event=event, user=user, tier=tier)

            # Make some invitees members
            if i % 2 == 0:
                OrganizationMember.objects.create(
                    organization=org,
                    user=user,
                    status=OrganizationMember.MembershipStatus.ACTIVE,
                )

            viewer_users.append(user)

        # Create some RSVPs
        for i, user in enumerate(attendee_users[:5]):
            EventRSVP.objects.create(
                event=event,
                user=user,
                status=EventRSVP.RsvpStatus.YES,
            )

        expected_queries = len(viewer_users) * len(attendee_users) * 5

        return BenchmarkScenario(
            name=name.upper(),
            description=(
                f"{len(viewer_users)} viewers, {len(attendee_users)} attendees, ~{expected_queries} queries worst case"
            ),
            organization=org,
            event=event,
            tier=tier,
            user=owner,
            extra_data={
                "viewers": viewer_users,
                "attendees": attendee_users,
                "viewer_count": len(viewer_users),
                "attendee_count": len(attendee_users),
                "expected_n1_queries": expected_queries,
            },
        )

    def _benchmark_resolve_visibility_n1(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark resolve_visibility with current N+1 pattern."""
        from events.service.user_preferences_service import resolve_visibility

        result = BenchmarkResult(name="resolve_visibility (N+1)", runs=runs)

        viewers = scenario.extra_data["viewers"]
        attendees = scenario.extra_data["attendees"]
        event = scenario.event
        owner_id = scenario.organization.owner_id
        staff_ids = {sm.id for sm in scenario.organization.staff_members.all()}

        # Warm-up run
        gc.collect()
        for viewer in viewers[:2]:
            for target in attendees[:2]:
                resolve_visibility(viewer, target, event, owner_id, staff_ids)

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Call resolve_visibility for each viewer×target pair (N+1 pattern)
            for viewer in viewers:
                for target in attendees:
                    resolve_visibility(viewer, target, event, owner_id, staff_ids)

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            # Show stats on first run
            if i == 0:
                pairs = len(viewers) * len(attendees)
                queries_per_pair = len(connection.queries) / pairs if pairs else 0
                self.stdout.write(f"    Pairs checked: {pairs}")
                self.stdout.write(f"    Queries per pair: {queries_per_pair:.1f}")
                self.stdout.write(
                    self.style.WARNING(f"    N+1 DETECTED: {len(connection.queries)} queries for {pairs} pairs")
                )

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def _benchmark_resolve_visibility_optimized(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark resolve_visibility_fast with OPTIMIZED prefetched context."""
        result = BenchmarkResult(name="resolve_visibility (OPTIMIZED)", runs=runs)

        viewers = scenario.extra_data["viewers"]
        attendees = scenario.extra_data["attendees"]
        event = scenario.event
        owner_id = scenario.organization.owner_id
        staff_ids = {sm.id for sm in scenario.organization.staff_members.all()}

        # Prefetch attendees with general_preferences for O(1) preference lookup
        attendees_with_prefs = list(
            RevelUser.objects.filter(id__in=[a.id for a in attendees]).select_related("general_preferences")
        )

        # Warm-up run
        gc.collect()
        reset_queries()

        # Create context ONCE (4 queries instead of 5N)
        _ = VisibilityContext.for_event(event, owner_id, staff_ids)

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Create context (4 queries for prefetching)
            context = VisibilityContext.for_event(event, owner_id, staff_ids)

            # Call resolve_visibility_fast for each viewer×target pair (O(1) per pair)
            for viewer in viewers:
                for target in attendees_with_prefs:
                    resolve_visibility_fast(viewer, target, context)

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            # Show stats on first run
            if i == 0:
                pairs = len(viewers) * len(attendees)
                query_count = len(connection.queries)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"    OPTIMIZED: {query_count} queries for {pairs} pairs (4 for context + 0 per pair)"
                    )
                )

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def _benchmark_full_visibility_task(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark full build_attendee_visibility_flags task (now uses optimized path)."""
        from events.tasks import build_attendee_visibility_flags

        result = BenchmarkResult(name="build_attendee_visibility_flags", runs=runs)

        event_id = str(scenario.event.pk)

        # Warm-up run
        gc.collect()
        try:
            with transaction.atomic():
                build_attendee_visibility_flags(event_id)
                raise Exception("Rollback")
        except Exception:
            pass  # Intentional rollback

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()
            queries_snapshot: list[dict[str, str]] = []

            try:
                with transaction.atomic():
                    build_attendee_visibility_flags(event_id)
                    queries_snapshot = list(connection.queries)
                    raise Exception("Rollback")
            except Exception:
                pass  # Intentional rollback

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(queries_snapshot) if queries_snapshot else len(connection.queries))

            # Show stats on first run
            if i == 0:
                query_count = len(queries_snapshot) if queries_snapshot else len(connection.queries)
                self.stdout.write(f"    Total queries: {query_count}")

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(queries_snapshot if queries_snapshot else list(connection.queries))

        return result
