"""Checkout endpoint benchmark.

Profiles:
- CanPurchaseTicket permission checks
- EligibilityService initialization and gate traversal
- BatchTicketService operations

Usage via run_benchmark command:
    python manage.py run_benchmark --checkout --runs 10
    python manage.py run_benchmark --checkout --runs 1 --query-breakdown

For Silk profiling (requires Silk to be enabled):
    python manage.py run_benchmark --checkout --runs 10 --silk
"""

import gc
import time
import typing as t

from django.db import connection, reset_queries, transaction

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    Event,
    EventInvitation,
    EventRSVP,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
    WhitelistRequest,
)
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

from .base import BaseBenchmarkCommand, BenchmarkResult, BenchmarkScenario


class CheckoutBenchmark(BaseBenchmarkCommand):
    """Benchmark the /checkout endpoint and related components."""

    help = "Benchmark the /checkout endpoint and related components."
    benchmark_name = "Checkout Endpoint"

    def add_extra_arguments(self, parser: t.Any) -> None:
        """Add checkout-specific arguments."""
        parser.add_argument(
            "--scenario",
            type=str,
            choices=["all", "minimal", "heavy", "gates"],
            default="all",
            help="Which scenarios to run",
        )

    def create_scenarios(self, options: dict[str, t.Any]) -> list[BenchmarkScenario]:
        """Create checkout benchmark scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Setting up benchmark scenarios ---"))
        scenarios: list[BenchmarkScenario] = []
        scenario_filter = options.get("scenario", "all")

        if scenario_filter in ("all", "minimal"):
            scenarios.append(self._create_minimal_scenario())

        if scenario_filter in ("all", "heavy"):
            scenarios.append(self._create_heavy_scenario())

        if scenario_filter in ("all", "gates"):
            scenarios.extend(self._create_gate_scenarios())

        return scenarios

    def run_benchmarks(self, scenarios: list[BenchmarkScenario], runs: int) -> dict[str, list[BenchmarkResult]]:
        """Run checkout benchmarks for all scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Running Benchmarks ---"))
        results: dict[str, list[BenchmarkResult]] = {}

        for scenario in scenarios:
            self.stdout.write(f"\n  Scenario: {scenario.name}")
            self.stdout.write(f"  Description: {scenario.description}")

            scenario_results: list[BenchmarkResult] = []

            # Benchmark 1: EligibilityService initialization + check
            result = self._benchmark_eligibility(scenario, runs)
            scenario_results.append(result)

            # Benchmark 2: Full checkout flow (without actual ticket creation)
            result = self._benchmark_full_checkout_flow(scenario, runs)
            scenario_results.append(result)

            # Benchmark 3: BatchTicketService.create_batch (with rollback)
            result = self._benchmark_batch_create(scenario, runs)
            scenario_results.append(result)

            results[scenario.name] = scenario_results

        return results

    def run_silk_profiling(self, scenarios: list[BenchmarkScenario]) -> None:
        """Profile checkout scenarios with Silk."""
        try:
            from silk.profiling.profiler import silk_profile  # type: ignore[import-untyped]
        except ImportError:
            return

        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService
        from events.service.event_manager import EventManager

        for scenario in scenarios:
            self._profile_scenario(scenario, silk_profile, EventManager, BatchTicketService, TicketPurchaseItem)

    def _profile_scenario(
        self,
        scenario: BenchmarkScenario,
        silk_profile: t.Any,
        event_manager_cls: t.Any,
        batch_service_cls: t.Any,
        ticket_item_cls: t.Any,
    ) -> None:
        """Profile a single scenario with Silk (reduces nesting depth)."""
        self.stdout.write(f"\n  Profiling scenario: {scenario.name}")

        # Profile eligibility check
        with silk_profile(name=f"Eligibility - {scenario.name}"):
            user = RevelUser.objects.get(pk=scenario.user.pk)
            event = Event.objects.get(pk=scenario.event.pk)
            manager = event_manager_cls(user, event)
            manager.check_eligibility()

        # Profile batch creation (with rollback)
        if scenario.tier:
            self._profile_batch_create(scenario, silk_profile, batch_service_cls, ticket_item_cls)

        self.stdout.write(f"    Created Silk profiles for {scenario.name}")

    def _profile_batch_create(
        self,
        scenario: BenchmarkScenario,
        silk_profile: t.Any,
        batch_service_cls: t.Any,
        ticket_item_cls: t.Any,
    ) -> None:
        """Profile batch creation with Silk (reduces nesting depth)."""
        items = [ticket_item_cls(guest_name="Silk Test")]
        try:
            with transaction.atomic():
                with silk_profile(name=f"BatchCreate - {scenario.name}"):
                    user = RevelUser.objects.get(pk=scenario.user.pk)
                    event = Event.objects.get(pk=scenario.event.pk)
                    tier = TicketTier.objects.get(pk=scenario.tier.pk)  # type: ignore[union-attr]
                    service = batch_service_cls(event, tier, user)
                    service.create_batch(items)
                raise Exception("Rollback")
        except Exception:
            pass  # Intentional rollback

    # --- Scenario Creation ---

    def _create_minimal_scenario(self) -> BenchmarkScenario:
        """Create a minimal scenario - baseline for comparison."""
        self.stdout.write("  Creating MINIMAL scenario...")

        owner = self.create_test_user("minimal_owner")
        org = self.create_test_organization("minimal", owner)
        event = self.create_test_event(org, "Minimal Event")
        tier = self.create_test_tier(event)
        user = self.create_test_user("minimal_user")

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

        owner = self.create_test_user("heavy_owner")
        org = self.create_test_organization("heavy", owner)

        # Populate with staff and members
        population = self.populate_organization(org, staff_count=20, member_count=100)
        members = population["members"]

        event = self.create_test_event(org, "Heavy Event", max_attendees=1000)

        # Create multiple ticket tiers
        tiers: list[TicketTier] = []
        for i in range(5):
            tier = self.create_test_tier(event, f"Tier {i}", price=i * 10)
            tiers.append(tier)

        # Create many existing tickets
        tickets: list[Ticket] = []
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
        rsvps: list[EventRSVP] = []
        for member in members[50:80]:
            rsvp = EventRSVP.objects.create(
                event=event,
                user=member,
                status=EventRSVP.RsvpStatus.YES,
            )
            rsvps.append(rsvp)

        # Create many invitations
        invitations: list[EventInvitation] = []
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
        user = self.create_test_user(
            "heavy_user",
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
            description="100 members, 20 staff, 50 tickets, 30 RSVPs, 20 invitations, 5 tiers, 1 questionnaire",
            organization=org,
            event=event,
            tier=tiers[0],
            user=user,
            extra_data={
                "staff_count": 20,
                "member_count": 100,
                "ticket_count": len(tickets),
                "rsvp_count": len(rsvps),
                "invitation_count": len(invitations),
                "tier_count": len(tiers),
            },
        )

    def _create_gate_scenarios(self) -> list[BenchmarkScenario]:
        """Create scenarios that exercise specific gates."""
        scenarios: list[BenchmarkScenario] = []

        self.stdout.write("  Creating BLACKLIST_FUZZY scenario...")
        scenarios.append(self._create_blacklist_fuzzy_scenario())

        self.stdout.write("  Creating PRIVATE_EVENT scenario...")
        scenarios.append(self._create_private_event_scenario())

        self.stdout.write("  Creating MEMBERS_ONLY scenario...")
        scenarios.append(self._create_members_only_scenario())

        self.stdout.write("  Creating FULL_PROFILE scenario...")
        scenarios.append(self._create_full_profile_scenario())

        self.stdout.write("  Creating QUESTIONNAIRE scenario...")
        scenarios.append(self._create_questionnaire_scenario())

        return scenarios

    def _create_blacklist_fuzzy_scenario(self) -> BenchmarkScenario:
        """Create scenario with fuzzy blacklist matching."""
        owner = self.create_test_user("bl_owner")
        org = self.create_test_organization("blacklist", owner)

        # Create many blacklist entries for fuzzy matching
        for i in range(50):
            Blacklist.objects.create(
                organization=org,
                first_name=f"Blocked{i}",
                last_name=f"Person{i}",
                reason="Test blacklist entry",
                created_by=owner,
            )

        event = self.create_test_event(org, "Blacklist Test Event")
        tier = self.create_test_tier(event)

        # User with similar name (triggers fuzzy matching but is whitelisted)
        user = self.create_test_user(
            "bl_user",
            first_name="Blocked0",
            last_name="Person0",
        )

        # Whitelist the user
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
        owner = self.create_test_user("priv_owner")
        org = self.create_test_organization("private", owner)

        event = self.create_test_event(
            org,
            "Private Event",
            event_type=Event.EventType.PRIVATE,
            visibility=Event.Visibility.PRIVATE,
            accept_invitation_requests=True,
        )
        tier = self.create_test_tier(event)

        user = self.create_test_user("priv_user")
        EventInvitation.objects.create(event=event, user=user, tier=tier)

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
        owner = self.create_test_user("mem_owner")
        org = self.create_test_organization("members", owner)

        event = self.create_test_event(
            org,
            "Members Only Event",
            event_type=Event.EventType.MEMBERS_ONLY,
            visibility=Event.Visibility.MEMBERS_ONLY,
        )
        tier = self.create_test_tier(event)

        user = self.create_test_user("mem_user")

        # Make user a member
        from events.models import OrganizationMember

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
        owner = self.create_test_user("prof_owner")
        org = self.create_test_organization("profile", owner)

        event = self.create_test_event(
            org,
            "Full Profile Event",
            requires_full_profile=True,
        )
        tier = self.create_test_tier(event)

        user = self.create_test_user(
            "prof_user",
            first_name="Full",
            last_name="Profile",
            pronouns="they/them",
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
        owner = self.create_test_user("quest_owner")
        org = self.create_test_organization("quest", owner)

        # Create multiple questionnaires
        questionnaires: list[Questionnaire] = []
        for i in range(3):
            q = Questionnaire.objects.create(
                name=f"Questionnaire {i}",
                status=Questionnaire.QuestionnaireStatus.PUBLISHED,
            )
            questionnaires.append(q)

        event = self.create_test_event(org, "Questionnaire Event")

        # Link questionnaires to event
        for q in questionnaires:
            org_q = OrganizationQuestionnaire.objects.create(
                organization=org,
                questionnaire=q,
                questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
            )
            org_q.events.add(event)

        tier = self.create_test_tier(event)
        user = self.create_test_user("quest_user")

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

    # --- Benchmark Methods ---

    def _benchmark_eligibility(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
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
                self.stdout.write(self.style.WARNING(f"    Warning: Eligibility failed - {eligibility.reason}"))

        return result

    def _benchmark_full_checkout_flow(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
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
        tier = TicketTier.objects.get(pk=scenario.tier.pk) if scenario.tier else None

        if tier:
            try:
                permission.has_object_permission(
                    MockRequest(user),  # type: ignore[arg-type]
                    MockController(),  # type: ignore[arg-type]
                    tier,
                )
            except Exception:
                pass  # Warmup errors don't affect measurements

        for i in range(runs):
            # Refresh objects from DB
            user = RevelUser.objects.get(pk=scenario.user.pk)
            event = Event.objects.get(pk=scenario.event.pk)
            tier = TicketTier.objects.get(pk=scenario.tier.pk) if scenario.tier else None

            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Phase 1: Permission check
            if tier:
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

    def _benchmark_batch_create(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark BatchTicketService.create_batch with transaction rollback."""
        from events.schema import TicketPurchaseItem
        from events.service.batch_ticket_service import BatchTicketService

        result = BenchmarkResult(name="BatchTicketService.create_batch", runs=runs)

        if not scenario.tier:
            return result

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
            pass  # Expected: warmup with intentional rollback

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()
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
                pass  # Expected: intentional rollback

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(queries_snapshot) if queries_snapshot else len(connection.queries))

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(queries_snapshot if queries_snapshot else list(connection.queries))

        return result
