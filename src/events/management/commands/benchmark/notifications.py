"""Notification dispatch benchmark.

Profiles notification-related operations that may have N+1 issues:
- Digest email sending with get_or_create loops
- Failed delivery retry with individual saves

Usage via run_benchmark command:
    python manage.py run_benchmark --notifications --runs 5
    python manage.py run_benchmark --notifications --runs 1 --query-breakdown
"""

import gc
import time
import typing as t

from django.db import connection, reset_queries, transaction
from django.utils import timezone

from accounts.models import RevelUser
from notifications.enums import DeliveryChannel, DeliveryStatus, NotificationType
from notifications.models import Notification, NotificationDelivery, NotificationPreference

from .base import BaseBenchmarkCommand, BenchmarkResult, BenchmarkScenario


class NotificationsBenchmark(BaseBenchmarkCommand):
    """Benchmark notification operations to identify N+1 issues."""

    help = "Benchmark notification dispatch (P3 N+1 issues)"
    benchmark_name = "Notification Dispatch"

    def add_extra_arguments(self, parser: t.Any) -> None:
        """Add notification-specific arguments."""
        parser.add_argument(
            "--notifications",
            type=int,
            default=20,
            help="Number of notifications to create per user",
        )
        parser.add_argument(
            "--users",
            type=int,
            default=5,
            help="Number of users to create",
        )
        parser.add_argument(
            "--scenario",
            type=str,
            choices=["all", "small", "medium", "large"],
            default="all",
            help="Which scenarios to run",
        )

    def create_scenarios(self, options: dict[str, t.Any]) -> list[BenchmarkScenario]:
        """Create notification benchmark scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Setting up benchmark scenarios ---"))
        scenarios: list[BenchmarkScenario] = []
        scenario_filter = options.get("scenario", "all")

        if scenario_filter == "all":
            scenarios.append(self._create_notification_scenario("small", users=3, notifications_per_user=5))
            scenarios.append(self._create_notification_scenario("medium", users=10, notifications_per_user=20))
            scenarios.append(self._create_notification_scenario("large", users=20, notifications_per_user=50))
        elif scenario_filter == "small":
            scenarios.append(self._create_notification_scenario("small", users=3, notifications_per_user=5))
        elif scenario_filter == "medium":
            scenarios.append(self._create_notification_scenario("medium", users=10, notifications_per_user=20))
        elif scenario_filter == "large":
            scenarios.append(self._create_notification_scenario("large", users=20, notifications_per_user=50))

        return scenarios

    def run_benchmarks(self, scenarios: list[BenchmarkScenario], runs: int) -> dict[str, list[BenchmarkResult]]:
        """Run notification benchmarks for all scenarios."""
        self.stdout.write(self.style.HTTP_INFO("\n--- Running Benchmarks ---"))
        results: dict[str, list[BenchmarkResult]] = {}

        for scenario in scenarios:
            self.stdout.write(f"\n  Scenario: {scenario.name}")
            self.stdout.write(f"  Description: {scenario.description}")

            scenario_results: list[BenchmarkResult] = []

            # Benchmark 1: Digest delivery marking with get_or_create (N+1)
            result = self._benchmark_digest_delivery_marking(scenario, runs)
            scenario_results.append(result)

            # Benchmark 2: Failed delivery retry with individual saves (N+1)
            result = self._benchmark_failed_delivery_retry(scenario, runs)
            scenario_results.append(result)

            # Benchmark 3: Digest delivery marking OPTIMIZED (bulk_create)
            result = self._benchmark_digest_delivery_optimized(scenario, runs)
            scenario_results.append(result)

            # Benchmark 4: Failed delivery retry OPTIMIZED (bulk_update)
            result = self._benchmark_failed_delivery_retry_optimized(scenario, runs)
            scenario_results.append(result)

            # Benchmark 5: Notification querying with preferences
            result = self._benchmark_notification_querying(scenario, runs)
            scenario_results.append(result)

            results[scenario.name] = scenario_results

        return results

    def _create_notification_scenario(
        self,
        name: str,
        *,
        users: int,
        notifications_per_user: int,
    ) -> BenchmarkScenario:
        """Create a notification scenario with varied data."""
        total_notifications = users * notifications_per_user
        self.stdout.write(f"  Creating {name.upper()} scenario ({users} users, {total_notifications} notifications)...")

        owner = self.create_test_user(f"notif_{name}_owner")
        org = self.create_test_organization(f"notif_{name}", owner)
        event = self.create_test_event(org, f"Notification {name.title()} Event")

        # Create users with notification preferences and notifications
        all_notifications: list[Notification] = []
        all_users: list[RevelUser] = []

        for user_idx in range(users):
            user = self.create_test_user(f"notif_{name}_user_{user_idx}")
            all_users.append(user)

            # Update notification preferences (may already exist from signal)
            pref, _ = NotificationPreference.objects.get_or_create(user=user)
            pref.digest_frequency = NotificationPreference.DigestFrequency.DAILY
            pref.digest_send_time = timezone.now().time()
            pref.save()

            # Create notifications for this user
            for notif_idx in range(notifications_per_user):
                notification = Notification.objects.create(
                    user=user,
                    notification_type=NotificationType.EVENT_UPDATED,
                    title=f"Test Notification {user_idx}-{notif_idx}",
                    body="Test notification body",
                    context={"event_id": str(event.pk)},
                )
                all_notifications.append(notification)

                # Create some failed deliveries for retry benchmark
                if notif_idx % 3 == 0:
                    NotificationDelivery.objects.create(
                        notification=notification,
                        channel=DeliveryChannel.EMAIL,
                        status=DeliveryStatus.FAILED,
                        retry_count=2,
                    )

        return BenchmarkScenario(
            name=name.upper(),
            description=f"{users} users, {len(all_notifications)} notifications",
            organization=org,
            event=event,
            user=owner,
            extra_data={
                "users": all_users,
                "notifications": all_notifications,
                "total_users": users,
                "total_notifications": len(all_notifications),
            },
        )

    def _benchmark_digest_delivery_marking(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark digest delivery marking with get_or_create loop (N+1 pattern)."""
        result = BenchmarkResult(name="Digest Delivery Marking (get_or_create)", runs=runs)

        notifications = scenario.extra_data["notifications"][:50]  # Limit for benchmark

        # Warm-up
        gc.collect()

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()
            queries_snapshot: list[dict[str, str]] = []

            try:
                with transaction.atomic():
                    # Simulate digest delivery marking (current N+1 pattern)
                    for notification in notifications:
                        NotificationDelivery.objects.get_or_create(
                            notification=notification,
                            channel=DeliveryChannel.EMAIL,
                            defaults={
                                "status": DeliveryStatus.SENT,
                                "delivered_at": timezone.now(),
                                "metadata": {"digest": True},
                            },
                        )

                    queries_snapshot = list(connection.queries)
                    raise Exception("Rollback")
            except Exception:
                pass  # Intentional rollback

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(queries_snapshot) if queries_snapshot else len(connection.queries))

            if i == 0:
                query_count = len(queries_snapshot) if queries_snapshot else len(connection.queries)
                queries_per_notification = query_count / len(notifications) if notifications else 0
                self.stdout.write(
                    f"    Queries per notification: {queries_per_notification:.1f} (expected: ~2 for get_or_create)"
                )
                if queries_per_notification > 1.5:
                    self.stdout.write(
                        self.style.WARNING(
                            f"    N+1 pattern: {query_count} queries for {len(notifications)} notifications"
                        )
                    )

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(queries_snapshot if queries_snapshot else list(connection.queries))

        return result

    def _benchmark_failed_delivery_retry(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark failed delivery retry with individual saves (N+1 pattern)."""
        result = BenchmarkResult(name="Failed Delivery Retry (individual saves)", runs=runs)

        # Get failed deliveries
        failed_deliveries = NotificationDelivery.objects.filter(
            status=DeliveryStatus.FAILED,
            notification__in=scenario.extra_data["notifications"],
        ).select_related("notification")[:30]

        if not failed_deliveries.exists():
            self.stdout.write(self.style.WARNING("    No failed deliveries to benchmark"))
            return result

        failed_list = list(failed_deliveries)

        # Warm-up
        gc.collect()

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()
            queries_snapshot: list[dict[str, str]] = []

            try:
                with transaction.atomic():
                    # Simulate failed delivery retry (current N+1 pattern)
                    for delivery in failed_list:
                        delivery.status = DeliveryStatus.PENDING
                        delivery.save(update_fields=["status", "updated_at"])

                    queries_snapshot = list(connection.queries)
                    raise Exception("Rollback")
            except Exception:
                pass  # Intentional rollback

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(queries_snapshot) if queries_snapshot else len(connection.queries))

            if i == 0:
                query_count = len(queries_snapshot) if queries_snapshot else len(connection.queries)
                queries_per_delivery = query_count / len(failed_list) if failed_list else 0
                self.stdout.write(f"    Queries per delivery update: {queries_per_delivery:.1f} (expected: 1 per save)")
                if queries_per_delivery > 1.5:
                    self.stdout.write(
                        self.style.WARNING(f"    N+1 pattern: {query_count} queries for {len(failed_list)} deliveries")
                    )

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(queries_snapshot if queries_snapshot else list(connection.queries))

        return result

    def _benchmark_digest_delivery_optimized(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark digest delivery marking with OPTIMIZED bulk_create pattern."""
        result = BenchmarkResult(name="Digest Delivery (OPTIMIZED bulk_create)", runs=runs)

        notifications = scenario.extra_data["notifications"][:50]  # Limit for benchmark

        # Warm-up
        gc.collect()

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()
            queries_snapshot: list[dict[str, str]] = []

            try:
                with transaction.atomic():
                    # OPTIMIZED: Use bulk operations instead of get_or_create loop
                    notification_ids = [n.id for n in notifications]

                    # 1 query: Find existing deliveries
                    existing_deliveries = set(
                        NotificationDelivery.objects.filter(
                            notification_id__in=notification_ids,
                            channel=DeliveryChannel.EMAIL,
                        ).values_list("notification_id", flat=True)
                    )

                    # 1 query: Bulk create missing deliveries
                    now = timezone.now()
                    deliveries_to_create = [
                        NotificationDelivery(
                            notification=notification,
                            channel=DeliveryChannel.EMAIL,
                            status=DeliveryStatus.SENT,
                            delivered_at=now,
                            metadata={"digest": True},
                        )
                        for notification in notifications
                        if notification.id not in existing_deliveries
                    ]

                    if deliveries_to_create:
                        NotificationDelivery.objects.bulk_create(deliveries_to_create)

                    queries_snapshot = list(connection.queries)
                    raise Exception("Rollback")
            except Exception:
                pass  # Intentional rollback

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(queries_snapshot) if queries_snapshot else len(connection.queries))

            if i == 0:
                query_count = len(queries_snapshot) if queries_snapshot else len(connection.queries)
                self.stdout.write(
                    self.style.SUCCESS(f"    OPTIMIZED: {query_count} queries for {len(notifications)} notifications")
                )

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(queries_snapshot if queries_snapshot else list(connection.queries))

        return result

    def _benchmark_failed_delivery_retry_optimized(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark failed delivery retry with OPTIMIZED bulk_update pattern."""
        result = BenchmarkResult(name="Failed Retry (OPTIMIZED bulk_update)", runs=runs)

        # Get failed deliveries
        failed_deliveries = NotificationDelivery.objects.filter(
            status=DeliveryStatus.FAILED,
            notification__in=scenario.extra_data["notifications"],
        ).select_related("notification")[:30]

        if not failed_deliveries.exists():
            self.stdout.write(self.style.WARNING("    No failed deliveries to benchmark"))
            return result

        failed_list = list(failed_deliveries)
        delivery_ids = [d.id for d in failed_list]

        # Warm-up
        gc.collect()

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()
            queries_snapshot: list[dict[str, str]] = []

            try:
                with transaction.atomic():
                    # OPTIMIZED: Single bulk update instead of loop
                    NotificationDelivery.objects.filter(id__in=delivery_ids).update(
                        status=DeliveryStatus.PENDING,
                        updated_at=timezone.now(),
                    )

                    queries_snapshot = list(connection.queries)
                    raise Exception("Rollback")
            except Exception:
                pass  # Intentional rollback

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(queries_snapshot) if queries_snapshot else len(connection.queries))

            if i == 0:
                query_count = len(queries_snapshot) if queries_snapshot else len(connection.queries)
                self.stdout.write(
                    self.style.SUCCESS(f"    OPTIMIZED: {query_count} queries for {len(failed_list)} deliveries")
                )

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(queries_snapshot if queries_snapshot else list(connection.queries))

        return result

    def _benchmark_notification_querying(self, scenario: BenchmarkScenario, runs: int) -> BenchmarkResult:
        """Benchmark notification querying with preferences."""
        result = BenchmarkResult(name="Notification Querying", runs=runs)

        users = scenario.extra_data["users"]

        # Warm-up
        gc.collect()
        for user in users[:2]:
            list(Notification.objects.filter(user=user).select_related("user")[:10])

        for i in range(runs):
            gc.collect()
            reset_queries()

            start = time.perf_counter()

            # Query notifications for each user (simulates digest preparation)
            for user in users:
                notifications = (
                    Notification.objects.filter(user=user, read_at__isnull=True)
                    .select_related("user")
                    .order_by("-created_at")[:20]
                )
                list(notifications)

            end = time.perf_counter()

            result.timings.append(end - start)
            result.query_counts.append(len(connection.queries))

            if i == 0 and self.query_breakdown:
                self._print_query_breakdown(connection.queries)

        return result
