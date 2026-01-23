"""Benchmark the /checkout endpoint and related components.

This command profiles:
- CanPurchaseTicket permission checks
- EligibilityService initialization and gate traversal
- BatchTicketService operations

Usage:
    python manage.py benchmark_checkout --runs 10

For Silk profiling (requires Silk to be enabled):
    python manage.py benchmark_checkout --runs 10 --silk

For detailed query breakdown:
    python manage.py benchmark_checkout --runs 1 --query-breakdown
"""

import gc
import re
import secrets
import statistics
import time
import typing as t
from dataclasses import dataclass, field
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, reset_queries, transaction
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    Event,
    EventInvitation,
    EventRSVP,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    OrganizationStaff,
    PermissionsSchema,
    Ticket,
    TicketTier,
    WhitelistRequest,
)
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

# Unique suffix for this run to avoid conflicts
RUN_ID = secrets.token_hex(4)


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run."""

    name: str
    runs: int
    timings: list[float] = field(default_factory=list)
    query_counts: list[int] = field(default_factory=list)

    @property
    def avg_time(self) -> float:
        """Average time in milliseconds."""
        return statistics.mean(self.timings) * 1000 if self.timings else 0

    @property
    def min_time(self) -> float:
        """Minimum time in milliseconds."""
        return min(self.timings) * 1000 if self.timings else 0

    @property
    def max_time(self) -> float:
        """Maximum time in milliseconds."""
        return max(self.timings) * 1000 if self.timings else 0

    @property
    def std_dev(self) -> float:
        """Standard deviation in milliseconds."""
        return statistics.stdev(self.timings) * 1000 if len(self.timings) > 1 else 0

    @property
    def avg_queries(self) -> float:
        """Average number of queries."""
        return statistics.mean(self.query_counts) if self.query_counts else 0


@dataclass
class BenchmarkScenario:
    """A benchmark scenario with setup data."""

    name: str
    description: str
    organization: Organization
    event: Event
    tier: TicketTier
    user: RevelUser
    extra_data: dict[str, t.Any] = field(default_factory=dict)


class Command(BaseCommand):
    help = "Benchmark the /checkout endpoint and related components."

    def add_arguments(self, parser: t.Any) -> None:
        """Add arguments to this command."""
        parser.add_argument(
            "--runs",
            type=int,
            default=10,
            help="Number of times to run each benchmark (default: 10)",
        )
        parser.add_argument(
            "--silk",
            action="store_true",
            help="Enable Silk profiling (requires silk to be installed and enabled)",
        )
        parser.add_argument(
            "--cleanup",
            action="store_true",
            default=True,
            help="Clean up test data after benchmarks (default: True)",
        )
        parser.add_argument(
            "--no-cleanup",
            action="store_false",
            dest="cleanup",
            help="Don't clean up test data after benchmarks",
        )
        parser.add_argument(
            "--scenario",
            type=str,
            choices=["all", "minimal", "heavy", "gates"],
            default="all",
            help="Which scenarios to run",
        )
        parser.add_argument(
            "--query-breakdown",
            action="store_true",
            help="Show detailed query breakdown (best with --runs 1)",
        )
        parser.add_argument(
            "--component-timing",
            action="store_true",
            help="Show component-level timing breakdown",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Handle the command."""
        runs = options["runs"]
        use_silk = options["silk"]
        cleanup = options["cleanup"]
        scenario_filter = options["scenario"]
        self.query_breakdown = options["query_breakdown"]
        self.component_timing = options["component_timing"]

        if runs <= 0:
            raise CommandError("--runs must be a positive integer.")

        self.stdout.write(self.style.HTTP_INFO("=" * 70))
        self.stdout.write(self.style.HTTP_INFO("CHECKOUT ENDPOINT BENCHMARK"))
        self.stdout.write(self.style.HTTP_INFO("=" * 70))
        self.stdout.write(f"Runs per benchmark: {runs}")
        self.stdout.write(f"Silk profiling: {'enabled' if use_silk else 'disabled'}")
        self.stdout.write(f"Query breakdown: {'enabled' if self.query_breakdown else 'disabled'}")
        self.stdout.write(f"Component timing: {'enabled' if self.component_timing else 'disabled'}")
        self.stdout.write("")

        scenarios: list[BenchmarkScenario] = []
        try:
            # Create test scenarios
            scenarios = self._create_scenarios(scenario_filter)

            # Run benchmarks
            results = self._run_benchmarks(scenarios, runs, use_silk)

            # Print results
            self._print_results(results)

            # Run Silk profiling if enabled
            if use_silk:
                self._run_silk_profiling(scenarios)

        finally:
            if cleanup:
                self._cleanup_scenarios(scenarios)
                self.stdout.write(self.style.SUCCESS("\nTest data cleaned up."))
            else:
                self.stdout.write(
                    self.style.WARNING("\nTest data NOT cleaned up. Run 'python manage.py flush' or delete manually.")
                )

    def _create_scenarios(self, scenario_filter: str) -> list[BenchmarkScenario]:
        """Create benchmark scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Setting up benchmark scenarios ---"))
        scenarios: list[BenchmarkScenario] = []

        if scenario_filter in ("all", "minimal"):
            scenarios.append(self._create_minimal_scenario())

        if scenario_filter in ("all", "heavy"):
            scenarios.append(self._create_heavy_scenario())

        if scenario_filter in ("all", "gates"):
            scenarios.extend(self._create_gate_scenarios())

        self.stdout.write(f"Created {len(scenarios)} scenarios.")
        return scenarios

    def _create_minimal_scenario(self) -> BenchmarkScenario:
        """Create a minimal scenario - baseline for comparison."""
        self.stdout.write("  Creating MINIMAL scenario...")

        owner = RevelUser.objects.create_user(
            username=f"bench_minimal_owner_{RUN_ID}@benchmark.test",
            email=f"bench_minimal_owner_{RUN_ID}@benchmark.test",
            password="password",
        )

        org = Organization.objects.create(
            name=f"Benchmark Minimal Org {RUN_ID}",
            slug=f"bench-minimal-org-{RUN_ID}",
            owner=owner,
        )

        event = Event.objects.create(
            organization=org,
            name="Minimal Event",
            slug="minimal-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
            max_attendees=100,
        )

        tier = TicketTier.objects.create(
            event=event,
            name="General",
            price=0,
            payment_method=TicketTier.PaymentMethod.FREE,
            total_quantity=100,
        )

        user = RevelUser.objects.create_user(
            username=f"bench_minimal_user_{RUN_ID}@benchmark.test",
            email=f"bench_minimal_user_{RUN_ID}@benchmark.test",
            password="password",
        )

        return BenchmarkScenario(
            name="MINIMAL",
            description="Public event, no questionnaires, no membership requirements",
            organization=org,
            event=event,
            tier=tier,
            user=user,
        )

    def _create_heavy_scenario(self) -> BenchmarkScenario:
        """Create a heavy scenario - worst case for prefetch."""
        self.stdout.write("  Creating HEAVY scenario...")

        owner = RevelUser.objects.create_user(
            username=f"bench_heavy_owner_{RUN_ID}@benchmark.test",
            email=f"bench_heavy_owner_{RUN_ID}@benchmark.test",
            password="password",
        )

        org = Organization.objects.create(
            name=f"Benchmark Heavy Org {RUN_ID}",
            slug=f"bench-heavy-org-{RUN_ID}",
            owner=owner,
        )

        # Create many staff members
        staff_users = []
        for i in range(20):
            staff_user = RevelUser.objects.create_user(
                username=f"bench_heavy_staff_{i}_{RUN_ID}@benchmark.test",
                email=f"bench_heavy_staff_{i}_{RUN_ID}@benchmark.test",
                password="password",
            )
            OrganizationStaff.objects.create(
                organization=org,
                user=staff_user,
                permissions=PermissionsSchema().model_dump(mode="json"),
            )
            staff_users.append(staff_user)

        # Create many members
        members = []
        for i in range(100):
            member = RevelUser.objects.create_user(
                username=f"bench_heavy_member_{i}_{RUN_ID}@benchmark.test",
                email=f"bench_heavy_member_{i}_{RUN_ID}@benchmark.test",
                password="password",
            )
            OrganizationMember.objects.create(
                organization=org,
                user=member,
                status=OrganizationMember.MembershipStatus.ACTIVE,
            )
            members.append(member)

        event = Event.objects.create(
            organization=org,
            name="Heavy Event",
            slug="heavy-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
            max_attendees=1000,
        )

        # Create multiple ticket tiers
        tiers = []
        for i in range(5):
            tier = TicketTier.objects.create(
                event=event,
                name=f"Tier {i}",
                price=i * 10,
                payment_method=TicketTier.PaymentMethod.FREE,
                total_quantity=200,
            )
            tiers.append(tier)

        # Create many existing tickets
        tickets = []
        for i, member in enumerate(members[:50]):
            ticket = Ticket.objects.create(
                event=event,
                tier=tiers[i % len(tiers)],
                user=member,
                guest_name=member.get_display_name(),
                status=Ticket.TicketStatus.ACTIVE,
            )
            tickets.append(ticket)

        # Create many RSVPs
        rsvps = []
        for member in members[50:80]:
            rsvp = EventRSVP.objects.create(
                event=event,
                user=member,
                status=EventRSVP.RsvpStatus.YES,
            )
            rsvps.append(rsvp)

        # Create many invitations
        invitations = []
        for member in members[80:]:
            invitation = EventInvitation.objects.create(
                event=event,
                user=member,
                tier=tiers[0],
            )
            invitations.append(invitation)

        # Create questionnaires
        questionnaire = Questionnaire.objects.create(
            name="Heavy Questionnaire",
            status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        org_questionnaire = OrganizationQuestionnaire.objects.create(
            organization=org,
            questionnaire=questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        org_questionnaire.events.add(event)

        # Test user (not a member, needs to go through all gates)
        user = RevelUser.objects.create_user(
            username=f"bench_heavy_user_{RUN_ID}@benchmark.test",
            email=f"bench_heavy_user_{RUN_ID}@benchmark.test",
            password="password",
            first_name="Heavy",
            last_name="User",
            preferred_name="Heavy User",
            pronouns="they/them",
        )

        # Give user approved questionnaire submission
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        QuestionnaireEvaluation.objects.create(
            submission=submission,
            status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
        )

        return BenchmarkScenario(
            name="HEAVY",
            description=("100 members, 20 staff, 50 tickets, 30 RSVPs, 20 invitations, 5 tiers, 1 questionnaire"),
            organization=org,
            event=event,
            tier=tiers[0],
            user=user,
            extra_data={
                "staff_count": len(staff_users),
                "member_count": len(members),
                "ticket_count": len(tickets),
                "rsvp_count": len(rsvps),
                "invitation_count": len(invitations),
                "tier_count": len(tiers),
            },
        )

    def _create_gate_scenarios(self) -> list[BenchmarkScenario]:
        """Create scenarios that exercise specific gates."""
        scenarios: list[BenchmarkScenario] = []

        # Blacklist fuzzy matching scenario
        self.stdout.write("  Creating BLACKLIST_FUZZY scenario...")
        scenarios.append(self._create_blacklist_fuzzy_scenario())

        # Private event with invitation scenario
        self.stdout.write("  Creating PRIVATE_EVENT scenario...")
        scenarios.append(self._create_private_event_scenario())

        # Members-only event scenario
        self.stdout.write("  Creating MEMBERS_ONLY scenario...")
        scenarios.append(self._create_members_only_scenario())

        # Full profile required scenario
        self.stdout.write("  Creating FULL_PROFILE scenario...")
        scenarios.append(self._create_full_profile_scenario())

        # Questionnaire required scenario
        self.stdout.write("  Creating QUESTIONNAIRE scenario...")
        scenarios.append(self._create_questionnaire_scenario())

        return scenarios

    def _create_blacklist_fuzzy_scenario(self) -> BenchmarkScenario:
        """Create scenario with fuzzy blacklist matching."""
        owner = RevelUser.objects.create_user(
            username=f"bench_bl_owner_{RUN_ID}@benchmark.test",
            email=f"bench_bl_owner_{RUN_ID}@benchmark.test",
            password="password",
        )

        org = Organization.objects.create(
            name=f"Benchmark Blacklist Org {RUN_ID}",
            slug=f"bench-blacklist-org-{RUN_ID}",
            owner=owner,
        )

        # Create many blacklist entries with names (for fuzzy matching)
        for i in range(50):
            Blacklist.objects.create(
                organization=org,
                first_name=f"Blocked{i}",
                last_name=f"Person{i}",
                reason="Test blacklist entry",
                created_by=owner,
            )

        event = Event.objects.create(
            organization=org,
            name="Blacklist Test Event",
            slug="blacklist-test-event",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
            max_attendees=100,
        )

        tier = TicketTier.objects.create(
            event=event,
            name="General",
            price=0,
            payment_method=TicketTier.PaymentMethod.FREE,
            total_quantity=100,
        )

        # User with similar name (triggers fuzzy matching but is whitelisted)
        user = RevelUser.objects.create_user(
            username=f"bench_bl_user_{RUN_ID}@benchmark.test",
            email=f"bench_bl_user_{RUN_ID}@benchmark.test",
            password="password",
            first_name="Blocked0",  # Similar to blacklist entry
            last_name="Person0",
        )

        # Whitelist the user (via approved WhitelistRequest)
        WhitelistRequest.objects.create(
            organization=org,
            user=user,
            status=WhitelistRequest.Status.APPROVED,
        )

        return BenchmarkScenario(
            name="BLACKLIST_FUZZY",
            description="50 blacklist entries, user with fuzzy match but whitelisted",
            organization=org,
            event=event,
            tier=tier,
            user=user,
        )

    def _create_private_event_scenario(self) -> BenchmarkScenario:
        """Create private event scenario with invitation."""
        owner = RevelUser.objects.create_user(
            username=f"bench_priv_owner_{RUN_ID}@benchmark.test",
            email=f"bench_priv_owner_{RUN_ID}@benchmark.test",
            password="password",
        )

        org = Organization.objects.create(
            name=f"Benchmark Private Org {RUN_ID}",
            slug=f"bench-private-org-{RUN_ID}",
            owner=owner,
        )

        event = Event.objects.create(
            organization=org,
            name="Private Event",
            slug="private-event-bench",
            event_type=Event.EventType.PRIVATE,
            visibility=Event.Visibility.PRIVATE,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
            max_attendees=100,
            accept_invitation_requests=True,
        )

        tier = TicketTier.objects.create(
            event=event,
            name="General",
            price=0,
            payment_method=TicketTier.PaymentMethod.FREE,
            total_quantity=100,
        )

        user = RevelUser.objects.create_user(
            username=f"bench_priv_user_{RUN_ID}@benchmark.test",
            email=f"bench_priv_user_{RUN_ID}@benchmark.test",
            password="password",
        )

        # Create invitation for user
        EventInvitation.objects.create(
            event=event,
            user=user,
            tier=tier,
        )

        return BenchmarkScenario(
            name="PRIVATE_EVENT",
            description="Private event requiring invitation",
            organization=org,
            event=event,
            tier=tier,
            user=user,
        )

    def _create_members_only_scenario(self) -> BenchmarkScenario:
        """Create members-only event scenario."""
        owner = RevelUser.objects.create_user(
            username=f"bench_mem_owner_{RUN_ID}@benchmark.test",
            email=f"bench_mem_owner_{RUN_ID}@benchmark.test",
            password="password",
        )

        org = Organization.objects.create(
            name=f"Benchmark Members Org {RUN_ID}",
            slug=f"bench-members-org-{RUN_ID}",
            owner=owner,
        )

        event = Event.objects.create(
            organization=org,
            name="Members Only Event",
            slug="members-only-event-bench",
            event_type=Event.EventType.MEMBERS_ONLY,
            visibility=Event.Visibility.MEMBERS_ONLY,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
            max_attendees=100,
        )

        tier = TicketTier.objects.create(
            event=event,
            name="General",
            price=0,
            payment_method=TicketTier.PaymentMethod.FREE,
            total_quantity=100,
        )

        user = RevelUser.objects.create_user(
            username=f"bench_mem_user_{RUN_ID}@benchmark.test",
            email=f"bench_mem_user_{RUN_ID}@benchmark.test",
            password="password",
        )

        # Make user a member
        OrganizationMember.objects.create(
            organization=org,
            user=user,
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )

        return BenchmarkScenario(
            name="MEMBERS_ONLY",
            description="Members-only event with active membership",
            organization=org,
            event=event,
            tier=tier,
            user=user,
        )

    def _create_full_profile_scenario(self) -> BenchmarkScenario:
        """Create scenario requiring full profile."""
        owner = RevelUser.objects.create_user(
            username=f"bench_prof_owner_{RUN_ID}@benchmark.test",
            email=f"bench_prof_owner_{RUN_ID}@benchmark.test",
            password="password",
        )

        org = Organization.objects.create(
            name=f"Benchmark Profile Org {RUN_ID}",
            slug=f"bench-profile-org-{RUN_ID}",
            owner=owner,
        )

        event = Event.objects.create(
            organization=org,
            name="Full Profile Event",
            slug="full-profile-event-bench",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
            requires_full_profile=True,
            max_attendees=100,
        )

        tier = TicketTier.objects.create(
            event=event,
            name="General",
            price=0,
            payment_method=TicketTier.PaymentMethod.FREE,
            total_quantity=100,
        )

        user = RevelUser.objects.create_user(
            username=f"bench_prof_user_{RUN_ID}@benchmark.test",
            email=f"bench_prof_user_{RUN_ID}@benchmark.test",
            password="password",
            first_name="Full",
            last_name="Profile",
            pronouns="they/them",
            # Note: profile_picture needs actual file, skip for benchmark
        )

        return BenchmarkScenario(
            name="FULL_PROFILE",
            description="Event requiring full profile (name, pronouns, picture)",
            organization=org,
            event=event,
            tier=tier,
            user=user,
        )

    def _create_questionnaire_scenario(self) -> BenchmarkScenario:
        """Create scenario with questionnaire requirement."""
        owner = RevelUser.objects.create_user(
            username=f"bench_quest_owner_{RUN_ID}@benchmark.test",
            email=f"bench_quest_owner_{RUN_ID}@benchmark.test",
            password="password",
        )

        org = Organization.objects.create(
            name=f"Benchmark Questionnaire Org {RUN_ID}",
            slug=f"bench-quest-org-{RUN_ID}",
            owner=owner,
        )

        # Create multiple questionnaires (worst case)
        questionnaires = []
        for i in range(3):
            q = Questionnaire.objects.create(
                name=f"Questionnaire {i}",
                status=Questionnaire.QuestionnaireStatus.PUBLISHED,
            )
            questionnaires.append(q)

        event = Event.objects.create(
            organization=org,
            name="Questionnaire Event",
            slug="questionnaire-event-bench",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + timedelta(days=7),
            end=timezone.now() + timedelta(days=8),
            requires_ticket=True,
            max_attendees=100,
        )

        # Link questionnaires to event
        for q in questionnaires:
            org_q = OrganizationQuestionnaire.objects.create(
                organization=org,
                questionnaire=q,
                questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
            )
            org_q.events.add(event)

        tier = TicketTier.objects.create(
            event=event,
            name="General",
            price=0,
            payment_method=TicketTier.PaymentMethod.FREE,
            total_quantity=100,
        )

        user = RevelUser.objects.create_user(
            username=f"bench_quest_user_{RUN_ID}@benchmark.test",
            email=f"bench_quest_user_{RUN_ID}@benchmark.test",
            password="password",
        )

        # Create approved submissions for all questionnaires
        for q in questionnaires:
            submission = QuestionnaireSubmission.objects.create(
                user=user,
                questionnaire=q,
                status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            )
            QuestionnaireEvaluation.objects.create(
                submission=submission,
                status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
            )

        return BenchmarkScenario(
            name="QUESTIONNAIRE",
            description="Event with 3 required questionnaires (all approved)",
            organization=org,
            event=event,
            tier=tier,
            user=user,
        )

    def _run_benchmarks(
        self, scenarios: list[BenchmarkScenario], runs: int, use_silk: bool
    ) -> dict[str, list[BenchmarkResult]]:
        """Run benchmarks for all scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Running Benchmarks ---"))

        results: dict[str, list[BenchmarkResult]] = {}

        for scenario in scenarios:
            self.stdout.write(f"\n  Scenario: {scenario.name}")
            self.stdout.write(f"  Description: {scenario.description}")

            scenario_results = []

            # Benchmark 1: EligibilityService initialization + check
            result = self._benchmark_eligibility(scenario, runs, use_silk)
            scenario_results.append(result)

            # Benchmark 2: Full checkout flow (without actual ticket creation)
            result = self._benchmark_full_checkout_flow(scenario, runs, use_silk)
            scenario_results.append(result)

            # Benchmark 3: BatchTicketService.create_batch (with rollback)
            result = self._benchmark_batch_create(scenario, runs, use_silk)
            scenario_results.append(result)

            results[scenario.name] = scenario_results

        return results

    def _benchmark_eligibility(self, scenario: BenchmarkScenario, runs: int, use_silk: bool) -> BenchmarkResult:
        """Benchmark EligibilityService initialization and check_eligibility."""
        from events.service.event_manager.service import EligibilityService

        result = BenchmarkResult(name="EligibilityService", runs=runs)

        # Warm-up run
        gc.collect()
        _ = EligibilityService(scenario.user, scenario.event).check_eligibility()

        for i in range(runs):
            # Refresh objects from DB to simulate real request
            user = RevelUser.objects.get(pk=scenario.user.pk)
            event = Event.objects.get(pk=scenario.event.pk)

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            service = EligibilityService(user, event)
            init_time = time.perf_counter()
            eligibility = service.check_eligibility()

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            # Show component timing on first run
            if i == 0 and self.component_timing:
                init_ms = (init_time - start) * 1000
                check_ms = (end - init_time) * 1000
                self.stdout.write("    Component timing:")
                self.stdout.write(f"      EligibilityService.__init__: {init_ms:.2f}ms")
                self.stdout.write(f"      check_eligibility (gates): {check_ms:.2f}ms")

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

            # Verify eligibility passed
            if not eligibility.allowed:
                self.stdout.write(self.style.WARNING(f"    Warning: Eligibility check failed - {eligibility.reason}"))

        return result

    def _benchmark_full_checkout_flow(self, scenario: BenchmarkScenario, runs: int, use_silk: bool) -> BenchmarkResult:
        """Benchmark full checkout flow without actual ticket creation."""
        from events.controllers.permissions import CanPurchaseTicket
        from events.service.event_manager import EventManager

        result = BenchmarkResult(name="Full Checkout Flow (no DB write)", runs=runs)

        class MockRequest:
            def __init__(self, user: RevelUser) -> None:
                self.user = user

        class MockController:
            pass

        permission = CanPurchaseTicket()

        # Warm-up run
        gc.collect()
        user = RevelUser.objects.get(pk=scenario.user.pk)
        event = Event.objects.get(pk=scenario.event.pk)
        tier = TicketTier.objects.get(pk=scenario.tier.pk)

        try:
            permission.has_object_permission(
                MockRequest(user),  # type: ignore[arg-type]
                MockController(),  # type: ignore[arg-type]
                tier,
            )
            EventManager(user, event).check_eligibility()
        except Exception:
            pass  # Warm-up errors don't affect benchmark measurements

        for i in range(runs):
            # Refresh objects from DB to simulate real request
            user = RevelUser.objects.get(pk=scenario.user.pk)
            event = Event.objects.get(pk=scenario.event.pk)
            tier = TicketTier.objects.get(pk=scenario.tier.pk)

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Phase 1: Permission check
            try:
                permission.has_object_permission(
                    MockRequest(user),  # type: ignore[arg-type]
                    MockController(),  # type: ignore[arg-type]
                    tier,
                )
            except Exception:
                pass  # Permission denied is expected for some scenarios

            perm_time = time.perf_counter()

            # Phase 2: EventManager eligibility check
            manager = EventManager(user, event)
            manager.check_eligibility()

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            # Show component timing on first run
            if i == 0 and self.component_timing:
                perm_ms = (perm_time - start) * 1000
                elig_ms = (end - perm_time) * 1000
                self.stdout.write("    Component timing:")
                self.stdout.write(f"      CanPurchaseTicket: {perm_ms:.2f}ms")
                self.stdout.write(f"      EventManager.check_eligibility: {elig_ms:.2f}ms")

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def _benchmark_batch_create(self, scenario: BenchmarkScenario, runs: int, use_silk: bool) -> BenchmarkResult:
        """Benchmark BatchTicketService.create_batch with transaction rollback."""
        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService

        result = BenchmarkResult(name="BatchTicketService.create_batch", runs=runs)

        items = [TicketPurchaseItem(guest_name="Test Guest")]

        # Warm-up run (with rollback)
        gc.collect()
        try:
            with transaction.atomic():
                user = RevelUser.objects.get(pk=scenario.user.pk)
                event = Event.objects.get(pk=scenario.event.pk)
                tier = TicketTier.objects.get(pk=scenario.tier.pk)
                service = BatchTicketService(event, tier, user)
                service.create_batch(items)
                raise Exception("Rollback")
        except Exception:
            pass  # Expected: warm-up run with intentional rollback

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Initialize before try block to ensure it's always defined
            queries_snapshot: list[dict[str, str]] = []

            try:
                with transaction.atomic():
                    # Refresh objects from DB
                    user = RevelUser.objects.get(pk=scenario.user.pk)
                    event = Event.objects.get(pk=scenario.event.pk)
                    tier = TicketTier.objects.get(pk=scenario.tier.pk)

                    service = BatchTicketService(event, tier, user)
                    service.create_batch(items)

                    # Capture queries before rollback
                    queries_snapshot = list(connection.queries)

                    # Force rollback to not actually create tickets
                    raise Exception("Rollback")
            except Exception:
                pass  # Expected: we intentionally raise to trigger rollback

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(queries_snapshot) if queries_snapshot else len(connection.queries))

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(queries_snapshot if queries_snapshot else list(connection.queries))

        return result

    def _print_results(self, results: dict[str, list[BenchmarkResult]]) -> None:
        """Print benchmark results in a formatted table."""
        self.stdout.write(self.style.HTTP_INFO("\n" + "=" * 70))
        self.stdout.write(self.style.HTTP_INFO("BENCHMARK RESULTS"))
        self.stdout.write(self.style.HTTP_INFO("=" * 70))

        for scenario_name, scenario_results in results.items():
            self.stdout.write(self.style.SUCCESS(f"\n{scenario_name}"))
            self.stdout.write("-" * 70)
            self.stdout.write(f"{'Benchmark':<35} {'Avg (ms)':<12} {'Min':<10} {'Max':<10} {'Queries':<8}")
            self.stdout.write("-" * 70)

            for result in scenario_results:
                self.stdout.write(
                    f"{result.name:<35} "
                    f"{result.avg_time:<12.2f} "
                    f"{result.min_time:<10.2f} "
                    f"{result.max_time:<10.2f} "
                    f"{result.avg_queries:<8.1f}"
                )

        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("SUMMARY")
        self.stdout.write("=" * 70)

        # Find heaviest scenarios
        all_results: list[tuple[str, BenchmarkResult]] = []
        for scenario_name, scenario_results in results.items():
            for result in scenario_results:
                all_results.append((scenario_name, result))

        # Sort by average time
        all_results.sort(key=lambda x: x[1].avg_time, reverse=True)

        self.stdout.write("\nSlowest operations:")
        for scenario_name, result in all_results[:5]:
            self.stdout.write(
                f"  {scenario_name}/{result.name}: {result.avg_time:.2f}ms ({result.avg_queries:.1f} queries)"
            )

        # Sort by query count
        all_results.sort(key=lambda x: x[1].avg_queries, reverse=True)

        self.stdout.write("\nMost queries:")
        for scenario_name, result in all_results[:5]:
            self.stdout.write(
                f"  {scenario_name}/{result.name}: {result.avg_queries:.1f} queries ({result.avg_time:.2f}ms)"
            )

    def _cleanup_scenarios(self, scenarios: list[BenchmarkScenario]) -> None:
        """Clean up all test data created for scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Cleaning up test data ---"))

        for scenario in scenarios:
            # Delete organization cascades to everything else
            try:
                scenario.organization.delete()
            except Exception as e:
                self.stdout.write(
                    self.style.WARNING(f"  Warning: Failed to delete org {scenario.organization.name}: {e}")
                )

            # Delete user if not already deleted
            try:
                scenario.user.delete()
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Warning: Failed to delete user {scenario.user.username}: {e}"))

    def _run_silk_profiling(self, scenarios: list[BenchmarkScenario]) -> None:
        """Run detailed Silk profiling for the checkout flow."""
        try:
            from silk.profiling.profiler import silk_profile  # type: ignore[import-untyped]
        except ImportError:
            self.stdout.write(self.style.WARNING("\nSilk is not installed. Install with: pip install django-silk"))
            return

        self.stdout.write(self.style.HTTP_INFO("\n" + "=" * 70))
        self.stdout.write(self.style.HTTP_INFO("SILK PROFILING"))
        self.stdout.write(self.style.HTTP_INFO("=" * 70))
        self.stdout.write(
            "Silk profiling is enabled. After running this command, "
            "visit /silk/ in your browser to view detailed profiles."
        )

        # Run one profile for each scenario
        for scenario in scenarios:
            self._silk_profile_scenario(scenario, silk_profile)

    def _silk_profile_scenario(self, scenario: BenchmarkScenario, silk_profile: t.Any) -> None:
        """Profile a single scenario with Silk."""
        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService
        from events.service.event_manager import EventManager

        self.stdout.write(f"\n  Profiling scenario: {scenario.name}")

        # Profile eligibility check
        with silk_profile(name=f"Eligibility - {scenario.name}"):
            user = RevelUser.objects.get(pk=scenario.user.pk)
            event = Event.objects.get(pk=scenario.event.pk)
            manager = EventManager(user, event)
            manager.check_eligibility()

        # Profile batch creation (with rollback)
        items = [TicketPurchaseItem(guest_name="Silk Test")]
        try:
            with transaction.atomic():
                with silk_profile(name=f"BatchCreate - {scenario.name}"):
                    user = RevelUser.objects.get(pk=scenario.user.pk)
                    event = Event.objects.get(pk=scenario.event.pk)
                    tier = TicketTier.objects.get(pk=scenario.tier.pk)
                    service = BatchTicketService(event, tier, user)
                    service.create_batch(items)
                raise Exception("Rollback")
        except Exception:
            pass  # Expected: intentional rollback to avoid creating actual tickets

        self.stdout.write(f"    Created Silk profiles for {scenario.name}")

    def _print_query_breakdown(self, queries: list[dict[str, t.Any]]) -> None:
        """Print a detailed breakdown of queries."""
        if not self.query_breakdown:
            return

        self.stdout.write(self.style.HTTP_INFO("\n    Query Breakdown:"))

        # Group queries by type and table
        query_groups: dict[str, list[dict[str, t.Any]]] = {}
        for q in queries:
            sql = q.get("sql", "")
            # Extract query type and table
            match = re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE)\s+", sql, re.IGNORECASE)
            query_type = match.group(1).upper() if match else "OTHER"

            # Try to extract table name
            table_match = re.search(r"(?:FROM|INTO|UPDATE)\s+[\"']?(\w+)[\"']?", sql, re.IGNORECASE)
            table = table_match.group(1) if table_match else "unknown"

            key = f"{query_type} {table}"
            if key not in query_groups:
                query_groups[key] = []
            query_groups[key].append(q)

        # Sort by count descending
        sorted_groups = sorted(query_groups.items(), key=lambda x: len(x[1]), reverse=True)

        for key, group_queries in sorted_groups[:15]:  # Top 15 query groups
            total_time = sum(float(q.get("time", 0)) for q in group_queries)
            self.stdout.write(f"      {key}: {len(group_queries)} queries, {total_time:.4f}s")

        if len(sorted_groups) > 15:
            self.stdout.write(f"      ... and {len(sorted_groups) - 15} more query groups")

    def _benchmark_with_component_timing(self, scenario: BenchmarkScenario) -> dict[str, float]:
        """Run benchmark with detailed component timing."""
        from events.controllers.permissions import CanPurchaseTicket
        from events.service.event_manager.service import EligibilityService

        timings: dict[str, float] = {}

        class MockRequest:
            def __init__(self, user: RevelUser) -> None:
                self.user = user

        class MockController:
            pass

        # Refresh objects
        user = RevelUser.objects.get(pk=scenario.user.pk)
        event = Event.objects.get(pk=scenario.event.pk)
        tier = TicketTier.objects.get(pk=scenario.tier.pk)

        # Time permission check
        permission = CanPurchaseTicket()
        start = time.perf_counter()
        try:
            permission.has_object_permission(
                MockRequest(user),  # type: ignore[arg-type]
                MockController(),  # type: ignore[arg-type]
                tier,
            )
        except Exception:
            pass  # Permission denied is expected for some scenarios
        timings["permission_check"] = (time.perf_counter() - start) * 1000

        # Time EligibilityService init
        start = time.perf_counter()
        service = EligibilityService(user, event)
        timings["eligibility_init"] = (time.perf_counter() - start) * 1000

        # Time gate traversal
        start = time.perf_counter()
        service.check_eligibility()
        timings["gate_traversal"] = (time.perf_counter() - start) * 1000

        return timings
