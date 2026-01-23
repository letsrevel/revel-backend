"""Unified benchmark command for profiling N+1 queries and performance.

This command provides a single entry point for all benchmark types:
- --visibility: Profile visibility flag building (P0 N+1 issue)
- --dashboard: Profile dashboard endpoints (P1 N+1 issues)
- --notifications: Profile notification dispatch (P3 N+1 issues)
- --checkout: Profile checkout endpoint and eligibility

Usage:
    python manage.py run_benchmark --visibility --runs 3
    python manage.py run_benchmark --dashboard --scenario large
    python manage.py run_benchmark --notifications --query-breakdown
    python manage.py run_benchmark --checkout --scenario heavy --runs 5
    python manage.py run_benchmark --all  # Run all benchmarks

Common options:
    --runs N           Number of times to run each benchmark (default: 10)
    --cleanup/--no-cleanup  Clean up test data after benchmarks
    --query-breakdown  Show detailed query breakdown (best with --runs 1)
    --component-timing Show component-level timing breakdown
    --silk             Enable Silk profiling (requires silk to be installed)
"""

import typing as t

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    """Unified benchmark command for all benchmark types."""

    help = "Run performance benchmarks for various components"

    def add_arguments(self, parser: t.Any) -> None:
        """Add benchmark type arguments."""
        # Benchmark type selection
        benchmark_group = parser.add_argument_group("Benchmark Types (select at least one)")
        benchmark_group.add_argument(
            "--visibility",
            action="store_true",
            help="Run visibility flag building benchmarks (P0 N+1 issue)",
        )
        benchmark_group.add_argument(
            "--dashboard",
            action="store_true",
            help="Run dashboard endpoint benchmarks (P1 N+1 issues)",
        )
        benchmark_group.add_argument(
            "--notifications",
            action="store_true",
            help="Run notification dispatch benchmarks (P3 N+1 issues)",
        )
        benchmark_group.add_argument(
            "--checkout",
            action="store_true",
            help="Run checkout endpoint benchmarks",
        )
        benchmark_group.add_argument(
            "--all",
            action="store_true",
            help="Run all benchmark types",
        )

        # Common options
        common_group = parser.add_argument_group("Common Options")
        common_group.add_argument(
            "--runs",
            type=int,
            default=10,
            help="Number of times to run each benchmark (default: 10)",
        )
        common_group.add_argument(
            "--cleanup",
            action="store_true",
            default=True,
            help="Clean up test data after benchmarks (default: True)",
        )
        common_group.add_argument(
            "--no-cleanup",
            action="store_false",
            dest="cleanup",
            help="Don't clean up test data after benchmarks",
        )
        common_group.add_argument(
            "--query-breakdown",
            action="store_true",
            help="Show detailed query breakdown (best with --runs 1)",
        )
        common_group.add_argument(
            "--component-timing",
            action="store_true",
            help="Show component-level timing breakdown",
        )
        common_group.add_argument(
            "--silk",
            action="store_true",
            help="Enable Silk profiling (requires silk to be installed)",
        )

        # Visibility-specific options
        vis_group = parser.add_argument_group("Visibility Options (--visibility)")
        vis_group.add_argument(
            "--viewers",
            type=int,
            default=20,
            help="Number of viewers (users who can see attendee list)",
        )
        vis_group.add_argument(
            "--attendees",
            type=int,
            default=10,
            help="Number of attendees (targets to check visibility for)",
        )

        # Dashboard-specific options
        dash_group = parser.add_argument_group("Dashboard Options (--dashboard)")
        dash_group.add_argument(
            "--orgs",
            type=int,
            default=5,
            help="Number of organizations to create",
        )
        dash_group.add_argument(
            "--events-per-org",
            type=int,
            default=3,
            help="Number of events per organization",
        )

        # Notifications-specific options
        notif_group = parser.add_argument_group("Notifications Options (--notifications)")
        notif_group.add_argument(
            "--notifications-count",
            type=int,
            default=20,
            dest="notification_count",
            help="Number of notifications to create per user",
        )
        notif_group.add_argument(
            "--users",
            type=int,
            default=5,
            help="Number of users to create",
        )

        # Scenario selection (shared)
        parser.add_argument(
            "--scenario",
            type=str,
            default="all",
            help="Which scenarios to run (options vary by benchmark type)",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Handle the command execution."""
        # Determine which benchmarks to run
        run_visibility = options["visibility"] or options["all"]
        run_dashboard = options["dashboard"] or options["all"]
        run_notifications = options["notifications"] or options["all"]
        run_checkout = options["checkout"] or options["all"]

        if not any([run_visibility, run_dashboard, run_notifications, run_checkout]):
            raise CommandError(
                "Please specify at least one benchmark type: "
                "--visibility, --dashboard, --notifications, --checkout, or --all"
            )

        # Import benchmark classes
        from .benchmark import (
            CheckoutBenchmark,
            DashboardBenchmark,
            NotificationsBenchmark,
            VisibilityBenchmark,
        )

        # Run selected benchmarks
        if run_visibility:
            self._run_benchmark(VisibilityBenchmark, "Visibility", options)

        if run_dashboard:
            self._run_benchmark(DashboardBenchmark, "Dashboard", options)

        if run_notifications:
            self._run_benchmark(NotificationsBenchmark, "Notifications", options)

        if run_checkout:
            self._run_benchmark(CheckoutBenchmark, "Checkout", options)

        self.stdout.write(self.style.SUCCESS("\nAll requested benchmarks completed."))

    def _run_benchmark(
        self,
        benchmark_class: type,
        name: str,
        options: dict[str, t.Any],
    ) -> None:
        """Run a specific benchmark."""
        self.stdout.write(self.style.HTTP_INFO(f"\n{'=' * 70}"))
        self.stdout.write(self.style.HTTP_INFO(f"Running {name} Benchmark"))
        self.stdout.write(self.style.HTTP_INFO("=" * 70))

        # Create and run the benchmark
        benchmark = benchmark_class(stdout=self.stdout, stderr=self.stderr)
        benchmark.handle(**options)
