"""Dashboard endpoints benchmark.

Profiles dashboard queries that may have N+1 issues:
- RSVPs/InvitationRequests with incomplete event prefetch (tags, city, org)
- Organization listings with member/staff prefetch

Usage via run_benchmark command:
    python manage.py run_benchmark --dashboard --runs 10
    python manage.py run_benchmark --dashboard --runs 1 --query-breakdown
"""

import gc
import time
import typing as t

from django.db import connection, reset_queries
from django.db.models import QuerySet

from accounts.models import RevelUser
from events.models import (
    Event,
    EventInvitation,
    EventInvitationRequest,
    EventRSVP,
    Organization,
    OrganizationMember,
    Ticket,
)

from .base import BaseBenchmarkCommand, BenchmarkResult, BenchmarkScenario


class DashboardBenchmark(BaseBenchmarkCommand):
    """Benchmark dashboard endpoints to identify N+1 issues."""

    help = "Benchmark dashboard endpoints (P1 N+1 issues)"
    benchmark_name = "Dashboard Endpoints"

    def add_extra_arguments(self, parser: t.Any) -> None:
        """Add dashboard-specific arguments."""
        parser.add_argument(
            "--orgs",
            type=int,
            default=5,
            help="Number of organizations to create",
        )
        parser.add_argument(
            "--events-per-org",
            type=int,
            default=3,
            help="Number of events per organization",
        )
        parser.add_argument(
            "--scenario",
            type=str,
            choices=["all", "small", "medium", "large"],
            default="all",
            help="Which scenarios to run",
        )

    def create_scenarios(self, options: dict[str, t.Any]) -> list[BenchmarkScenario]:
        """Create dashboard benchmark scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Setting up benchmark scenarios ---"))
        scenarios: list[BenchmarkScenario] = []
        scenario_filter = options.get("scenario", "all")

        if scenario_filter == "all":
            scenarios.append(self._create_dashboard_scenario("small", orgs=3, events_per_org=2))
            scenarios.append(self._create_dashboard_scenario("medium", orgs=5, events_per_org=5))
            scenarios.append(self._create_dashboard_scenario("large", orgs=10, events_per_org=10))
        elif scenario_filter == "small":
            scenarios.append(self._create_dashboard_scenario("small", orgs=3, events_per_org=2))
        elif scenario_filter == "medium":
            scenarios.append(self._create_dashboard_scenario("medium", orgs=5, events_per_org=5))
        elif scenario_filter == "large":
            scenarios.append(self._create_dashboard_scenario("large", orgs=10, events_per_org=10))

        return scenarios

    def run_benchmarks(self, scenarios: list[BenchmarkScenario], runs: int) -> dict[str, list[BenchmarkResult]]:
        """Run dashboard benchmarks for all scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Running Benchmarks ---"))
        results: dict[str, list[BenchmarkResult]] = {}

        for scenario in scenarios:
            self.stdout.write(f"\n  Scenario: {scenario.name}")
            self.stdout.write(f"  Description: {scenario.description}")

            scenario_results: list[BenchmarkResult] = []

            # Benchmark 1: Dashboard events (uses .full())
            result = self._benchmark_dashboard_events(scenario, runs)
            scenario_results.append(result)

            # Benchmark 2: Dashboard RSVPs (potential N+1)
            result = self._benchmark_dashboard_rsvps(scenario, runs)
            scenario_results.append(result)

            # Benchmark 3: Dashboard invitation requests (potential N+1)
            result = self._benchmark_dashboard_invitation_requests(scenario, runs)
            scenario_results.append(result)

            # Benchmark 4: Dashboard organizations
            result = self._benchmark_dashboard_organizations(scenario, runs)
            scenario_results.append(result)

            # Benchmark 5: Dashboard tickets
            result = self._benchmark_dashboard_tickets(scenario, runs)
            scenario_results.append(result)

            results[scenario.name] = scenario_results

        return results

    def _create_dashboard_scenario(
        self,
        name: str,
        *,
        orgs: int,
        events_per_org: int,
    ) -> BenchmarkScenario:
        """Create a dashboard scenario with varied data."""
        self.stdout.write(f"  Creating {name.upper()} scenario ({orgs} orgs, {events_per_org} events each)...")

        # Create the dashboard user
        dashboard_user = self.create_test_user(f"dash_{name}_user")

        # Track first org/event for scenario
        first_org: Organization | None = None
        first_event: Event | None = None
        total_events = 0
        total_rsvps = 0
        total_tickets = 0
        total_invitations = 0

        for org_idx in range(orgs):
            # Create org owner
            owner = self.create_test_user(f"dash_{name}_owner_{org_idx}")
            org = self.create_test_organization(f"dash_{name}_{org_idx}", owner)

            if first_org is None:
                first_org = org

            # Populate with some staff and members
            self.populate_organization(org, staff_count=2, member_count=5)

            # Make dashboard user a member of some orgs
            if org_idx % 2 == 0:
                OrganizationMember.objects.create(
                    organization=org,
                    user=dashboard_user,
                    status=OrganizationMember.MembershipStatus.ACTIVE,
                )

            for event_idx in range(events_per_org):
                event = self.create_test_event(org, f"Event {org_idx}-{event_idx}")
                tier = self.create_test_tier(event)
                total_events += 1

                if first_event is None:
                    first_event = event

                # Give dashboard user varied relationships to events
                rel_type = (org_idx + event_idx) % 4
                if rel_type == 0:
                    # RSVP
                    EventRSVP.objects.create(
                        event=event,
                        user=dashboard_user,
                        status=EventRSVP.RsvpStatus.YES,
                    )
                    total_rsvps += 1
                elif rel_type == 1:
                    # Ticket
                    Ticket.objects.create(
                        event=event,
                        tier=tier,
                        user=dashboard_user,
                        guest_name=dashboard_user.get_display_name(),
                        status=Ticket.TicketStatus.ACTIVE,
                    )
                    total_tickets += 1
                elif rel_type == 2:
                    # Invitation
                    EventInvitation.objects.create(
                        event=event,
                        user=dashboard_user,
                        tier=tier,
                    )
                    total_invitations += 1
                else:
                    # Invitation request
                    EventInvitationRequest.objects.create(
                        event=event,
                        user=dashboard_user,
                    )

        assert first_org is not None
        assert first_event is not None

        return BenchmarkScenario(
            name=name.upper(),
            description=(
                f"{orgs} orgs, {total_events} events, {total_rsvps} RSVPs, "
                f"{total_tickets} tickets, {total_invitations} invitations"
            ),
            organization=first_org,
            event=first_event,
            user=dashboard_user,
            extra_data={
                "total_events": total_events,
                "total_rsvps": total_rsvps,
                "total_tickets": total_tickets,
                "total_invitations": total_invitations,
            },
        )

    def _benchmark_dashboard_events(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark dashboard events query."""
        result = BenchmarkResult(name="Dashboard Events (.full())", runs=runs)
        user = scenario.user

        # Warm-up - use queryset chain since .full() is only on Manager
        gc.collect()
        list(Event.objects.for_user(user).with_organization().with_city().with_tags().with_venue()[:10])

        for i in range(runs):
            user = RevelUser.objects.get(pk=scenario.user.pk)

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Simulate dashboard_events endpoint - chain queryset methods
            events = Event.objects.for_user(user).with_organization().with_city().with_tags().with_venue()[:20]
            # Force evaluation
            list(events)

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def _benchmark_dashboard_rsvps(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark dashboard RSVPs query - potential N+1 on event relations."""
        result = BenchmarkResult(name="Dashboard RSVPs (select_related event)", runs=runs)

        # Warm-up
        gc.collect()
        list(EventRSVP.objects.select_related("event").filter(user=scenario.user).order_by("-created_at")[:10])

        for i in range(runs):
            user = RevelUser.objects.get(pk=scenario.user.pk)

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Simulate dashboard_rsvps endpoint (current implementation)
            rsvps = EventRSVP.objects.select_related("event").filter(user=user).order_by("-created_at")[:20]
            # Force evaluation and access nested fields (triggers N+1)
            for rsvp in rsvps:
                _ = rsvp.event.organization_id  # Should be OK with select_related
                # These may cause N+1 if event.tags or event.city aren't prefetched:
                # _ = list(rsvp.event.tags.all())  # N+1!

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def _benchmark_dashboard_invitation_requests(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark dashboard invitation requests - potential N+1."""
        result = BenchmarkResult(name="Dashboard InvitationRequests", runs=runs)

        # Warm-up
        gc.collect()
        list(EventInvitationRequest.objects.select_related("event").filter(user=scenario.user)[:10])

        for i in range(runs):
            user = RevelUser.objects.get(pk=scenario.user.pk)

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Simulate dashboard_invitation_requests endpoint
            requests = EventInvitationRequest.objects.select_related("event").filter(user=user)[:20]
            # Force evaluation
            list(requests)

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def _benchmark_dashboard_organizations(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark dashboard organizations query."""
        result = BenchmarkResult(name="Dashboard Organizations", runs=runs)

        # Warm-up
        gc.collect()
        list(Organization.objects.for_user(scenario.user)[:10])

        for i in range(runs):
            user = RevelUser.objects.get(pk=scenario.user.pk)

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Simulate dashboard_organizations endpoint
            orgs = Organization.objects.for_user(user)[:20]
            # Force evaluation
            list(orgs)

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def _benchmark_dashboard_tickets(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark dashboard tickets query."""
        result = BenchmarkResult(name="Dashboard Tickets (.full())", runs=runs)

        # Warm-up
        gc.collect()
        list(Ticket.objects.full().filter(user=scenario.user)[:10])

        for i in range(runs):
            user = RevelUser.objects.get(pk=scenario.user.pk)

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Simulate dashboard_tickets endpoint
            tickets: QuerySet[Ticket] = Ticket.objects.full().filter(user=user)[:20]
            # Force evaluation
            list(tickets)

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result
