"""Base classes and utilities for benchmark commands.

This module provides the foundational classes for creating benchmark commands:
- BenchmarkResult: Captures timing statistics for a benchmark run
- BenchmarkScenario: Defines a test scenario with pre-loaded data
- BaseBenchmarkCommand: Base class with common benchmark functionality
"""

import gc
import secrets
import statistics
import time
import typing as t
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, reset_queries
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionsSchema,
    TicketTier,
)


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run with timing and query statistics."""

    name: str
    runs: int
    timings: list[float] = field(default_factory=list)
    query_counts: list[int] = field(default_factory=list)

    @property
    def avg_time_ms(self) -> float:
        """Average time in milliseconds."""
        return statistics.mean(self.timings) * 1000 if self.timings else 0

    @property
    def min_time_ms(self) -> float:
        """Minimum time in milliseconds."""
        return min(self.timings) * 1000 if self.timings else 0

    @property
    def max_time_ms(self) -> float:
        """Maximum time in milliseconds."""
        return max(self.timings) * 1000 if self.timings else 0

    @property
    def std_dev_ms(self) -> float:
        """Standard deviation in milliseconds."""
        return statistics.stdev(self.timings) * 1000 if len(self.timings) > 1 else 0

    @property
    def avg_queries(self) -> float:
        """Average number of queries."""
        return statistics.mean(self.query_counts) if self.query_counts else 0

    @property
    def total_time_ms(self) -> float:
        """Total time across all runs in milliseconds."""
        return sum(self.timings) * 1000 if self.timings else 0


@dataclass
class BenchmarkScenario:
    """A benchmark scenario with setup data for testing specific code paths."""

    name: str
    description: str
    organization: Organization
    event: Event
    user: RevelUser
    tier: TicketTier | None = None
    extra_data: dict[str, t.Any] = field(default_factory=dict)

    def cleanup(self) -> list[str]:
        """Clean up scenario data. Returns list of warnings if any."""
        warnings: list[str] = []

        try:
            self.organization.delete()
        except Exception as e:
            warnings.append(f"Failed to delete org {self.organization.name}: {e}")

        try:
            self.user.delete()
        except Exception as e:
            warnings.append(f"Failed to delete user {self.user.username}: {e}")

        return warnings


class BaseBenchmarkCommand(BaseCommand):
    """Base class for benchmark management commands.

    Provides common functionality for:
    - Argument parsing (--runs, --cleanup, --query-breakdown, etc.)
    - Scenario setup and teardown
    - Timing and query counting
    - Result formatting and reporting
    """

    # Unique suffix for this run to avoid conflicts with concurrent runs
    run_id: str = secrets.token_hex(4)

    # Subclasses should set this
    benchmark_name: str = "Benchmark"

    def add_arguments(self, parser: t.Any) -> None:
        """Add common benchmark arguments."""
        parser.add_argument(
            "--runs",
            type=int,
            default=10,
            help="Number of times to run each benchmark (default: 10)",
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
            "--query-breakdown",
            action="store_true",
            help="Show detailed query breakdown (best with --runs 1)",
        )
        parser.add_argument(
            "--component-timing",
            action="store_true",
            help="Show component-level timing breakdown",
        )
        parser.add_argument(
            "--silk",
            action="store_true",
            help="Enable Silk profiling (requires silk to be installed)",
        )
        # Allow subclasses to add their own arguments
        self.add_extra_arguments(parser)

    def add_extra_arguments(self, parser: t.Any) -> None:
        """Override in subclass to add additional arguments."""

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Handle the command execution."""
        runs = options["runs"]
        cleanup = options["cleanup"]
        self.query_breakdown = options["query_breakdown"]
        self.component_timing = options["component_timing"]
        self.use_silk = options["silk"]

        if runs <= 0:
            raise CommandError("--runs must be a positive integer.")

        self._print_header(runs)

        scenarios: list[BenchmarkScenario] = []
        try:
            # Create test scenarios
            scenarios = self.create_scenarios(options)
            self.stdout.write(f"Created {len(scenarios)} scenarios.\n")

            # Run benchmarks
            results = self.run_benchmarks(scenarios, runs)

            # Print results
            self._print_results(results)

            # Run Silk profiling if enabled
            if self.use_silk:
                self._run_silk_profiling(scenarios)

        finally:
            if cleanup:
                self._cleanup_scenarios(scenarios)
                self.stdout.write(self.style.SUCCESS("\nTest data cleaned up."))
            else:
                self.stdout.write(self.style.WARNING("\nTest data NOT cleaned up. Delete manually if needed."))

    @abstractmethod
    def create_scenarios(self, options: dict[str, t.Any]) -> list[BenchmarkScenario]:
        """Create benchmark scenarios. Override in subclass."""
        raise NotImplementedError

    @abstractmethod
    def run_benchmarks(self, scenarios: list[BenchmarkScenario], runs: int) -> dict[str, list[BenchmarkResult]]:
        """Run benchmarks for scenarios. Override in subclass."""
        raise NotImplementedError

    def run_silk_profiling(self, scenarios: list[BenchmarkScenario]) -> None:
        """Optional: Override to add Silk profiling."""

    # --- Helper methods for subclasses ---

    def time_operation(
        self,
        name: str,
        operation: t.Callable[[], t.Any],
        runs: int,
        *,
        warmup: bool = True,
        refresh_callback: t.Callable[[], None] | None = None,
    ) -> BenchmarkResult:
        """Time an operation multiple times and return results.

        Args:
            name: Name of the benchmark
            operation: Zero-arg callable to time
            runs: Number of runs
            warmup: Whether to do a warmup run first
            refresh_callback: Optional callback to refresh objects between runs
        """
        result = BenchmarkResult(name=name, runs=runs)

        # Warm-up run
        if warmup:
            gc.collect()
            try:
                operation()
            except Exception:
                pass  # Warmup errors don't affect measurements

        for i in range(runs):
            if refresh_callback:
                refresh_callback()

            gc.collect()
            reset_queries()

            start = time.perf_counter()
            try:
                operation()
            except Exception:
                pass  # We're measuring performance, not correctness
            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            # Show query breakdown on first run
            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result

    def create_test_user(self, prefix: str, **extra_fields: t.Any) -> RevelUser:
        """Create a test user with unique email/username."""
        email = f"bench_{prefix}_{self.run_id}@benchmark.test"
        return RevelUser.objects.create_user(
            username=email,
            email=email,
            password="password",
            **extra_fields,
        )

    def create_test_organization(self, prefix: str, owner: RevelUser) -> Organization:
        """Create a test organization with unique slug."""
        return Organization.objects.create(
            name=f"Benchmark {prefix.title()} Org {self.run_id}",
            slug=f"bench-{prefix}-org-{self.run_id}",
            owner=owner,
        )

    def create_test_event(
        self,
        org: Organization,
        name: str,
        **extra_fields: t.Any,
    ) -> Event:
        """Create a test event with sensible defaults."""
        defaults = {
            "organization": org,
            "name": name,
            "slug": f"{name.lower().replace(' ', '-')}-{self.run_id}",
            "event_type": Event.EventType.PUBLIC,
            "visibility": Event.Visibility.PUBLIC,
            "status": Event.EventStatus.OPEN,
            "start": timezone.now() + timedelta(days=7),
            "end": timezone.now() + timedelta(days=8),
            "requires_ticket": True,
            "max_attendees": 100,
        }
        defaults.update(extra_fields)
        return Event.objects.create(**defaults)

    def create_test_tier(
        self,
        event: Event,
        name: str = "General",
        **extra_fields: t.Any,
    ) -> TicketTier:
        """Create a test ticket tier."""
        defaults = {
            "event": event,
            "name": name,
            "price": 0,
            "payment_method": TicketTier.PaymentMethod.FREE,
            "total_quantity": 100,
        }
        defaults.update(extra_fields)
        return TicketTier.objects.create(**defaults)

    def populate_organization(
        self,
        org: Organization,
        *,
        staff_count: int = 0,
        member_count: int = 0,
    ) -> dict[str, list[RevelUser]]:
        """Populate organization with staff and members."""
        result: dict[str, list[RevelUser]] = {"staff": [], "members": []}

        for i in range(staff_count):
            user = self.create_test_user(f"{org.slug}_staff_{i}")
            OrganizationStaff.objects.create(
                organization=org,
                user=user,
                permissions=PermissionsSchema().model_dump(mode="json"),
            )
            result["staff"].append(user)

        for i in range(member_count):
            user = self.create_test_user(f"{org.slug}_member_{i}")
            OrganizationMember.objects.create(
                organization=org,
                user=user,
                status=OrganizationMember.MembershipStatus.ACTIVE,
            )
            result["members"].append(user)

        return result

    # --- Private methods ---

    def _print_header(self, runs: int) -> None:
        """Print benchmark header."""
        self.stdout.write(self.style.HTTP_INFO("=" * 70))
        self.stdout.write(self.style.HTTP_INFO(f"{self.benchmark_name.upper()} BENCHMARK"))
        self.stdout.write(self.style.HTTP_INFO("=" * 70))
        self.stdout.write(f"Runs per benchmark: {runs}")
        self.stdout.write(f"Silk profiling: {'enabled' if self.use_silk else 'disabled'}")
        self.stdout.write(f"Query breakdown: {'enabled' if self.query_breakdown else 'disabled'}")
        self.stdout.write(f"Component timing: {'enabled' if self.component_timing else 'disabled'}")
        self.stdout.write("")

    def _print_results(self, results: dict[str, list[BenchmarkResult]]) -> None:
        """Print benchmark results in formatted tables."""
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
                    f"{result.avg_time_ms:<12.2f} "
                    f"{result.min_time_ms:<10.2f} "
                    f"{result.max_time_ms:<10.2f} "
                    f"{result.avg_queries:<8.1f}"
                )

        # Summary
        self._print_summary(results)

    def _print_summary(self, results: dict[str, list[BenchmarkResult]]) -> None:
        """Print summary of benchmark results."""
        self.stdout.write("\n" + "=" * 70)
        self.stdout.write("SUMMARY")
        self.stdout.write("=" * 70)

        all_results: list[tuple[str, BenchmarkResult]] = []
        for scenario_name, scenario_results in results.items():
            for result in scenario_results:
                all_results.append((scenario_name, result))

        # Slowest operations
        all_results.sort(key=lambda x: x[1].avg_time_ms, reverse=True)
        self.stdout.write("\nSlowest operations:")
        for scenario_name, result in all_results[:5]:
            self.stdout.write(
                f"  {scenario_name}/{result.name}: {result.avg_time_ms:.2f}ms ({result.avg_queries:.1f} queries)"
            )

        # Most queries
        all_results.sort(key=lambda x: x[1].avg_queries, reverse=True)
        self.stdout.write("\nMost queries:")
        for scenario_name, result in all_results[:5]:
            self.stdout.write(
                f"  {scenario_name}/{result.name}: {result.avg_queries:.1f} queries ({result.avg_time_ms:.2f}ms)"
            )

    def _print_query_breakdown(self, queries: list[dict[str, t.Any]]) -> None:
        """Print query breakdown using utility function."""
        from .query_utils import format_query_breakdown

        breakdown = format_query_breakdown(queries)
        self.stdout.write(self.style.HTTP_INFO("\n    Query Breakdown:"))
        for line in breakdown:
            self.stdout.write(f"      {line}")

    def _cleanup_scenarios(self, scenarios: list[BenchmarkScenario]) -> None:
        """Clean up all test data."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Cleaning up test data ---"))

        for scenario in scenarios:
            warnings = scenario.cleanup()
            for warning in warnings:
                self.stdout.write(self.style.WARNING(f"  Warning: {warning}"))

    def _run_silk_profiling(self, scenarios: list[BenchmarkScenario]) -> None:
        """Run Silk profiling if available."""
        import importlib.util

        if importlib.util.find_spec("silk") is None:
            self.stdout.write(self.style.WARNING("\nSilk is not installed. Install with: uv add django-silk"))
            return

        self.stdout.write(self.style.HTTP_INFO("\n" + "=" * 70))
        self.stdout.write(self.style.HTTP_INFO("SILK PROFILING"))
        self.stdout.write(self.style.HTTP_INFO("=" * 70))
        self.stdout.write(
            "Silk profiling is enabled. After running this command, "
            "visit /silk/ in your browser to view detailed profiles."
        )

        self.run_silk_profiling(scenarios)
