# src/common/management/commands/benchmark.py

import time
import typing as t

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, reset_queries

from accounts.models import RevelUser
from events.models import Organization


class Command(BaseCommand):
    help = "Benchmark the performance of various QuerySet methods."

    def add_arguments(self, parser: t.Any) -> None:
        """Add arguments to this command."""
        parser.add_argument(
            "--user-type",
            type=str,
            choices=["owner", "staff", "member"],
            default="owner",
            help="The type of user to run the benchmark for.",
        )
        parser.add_argument(
            "--runs",
            type=int,
            default=10,
            help="The number of times to run the benchmark to get an average.",
        )

    def handle(self, *args: t.Any, **options: t.Any) -> None:
        """Handle."""
        user_type = options["user_type"]
        runs = options["runs"]

        if runs <= 0:
            raise CommandError("--runs must be a positive integer.")

        self.stdout.write(self.style.HTTP_INFO(f"--- Preparing benchmark for user type: '{user_type}' ---"))

        # Find a sample user of the specified type
        try:
            if user_type == "owner":
                user = RevelUser.objects.get(username="owner_org_1@example.com")
            elif user_type == "staff":
                user = RevelUser.objects.get(username="staff_1_org_1@example.com")
            else:  # member
                user = RevelUser.objects.get(username="member_1@example.com")
        except RevelUser.DoesNotExist:
            raise CommandError(
                f"Could not find a sample user for type '{user_type}'. Please run the `seed` command first."
            )

        self.stdout.write(f"Found user: {user.username}")
        self.stdout.write(f"Benchmark will execute {runs} times.")

        # Define the function we want to test
        # We wrap it in a lambda to pass it to our generic benchmark runner
        benchmark_func = lambda: list(Organization.objects.for_user(user))  # noqa: E731

        # Run and print the results
        self._run_benchmark(
            title=f"Organization.objects.for_user() for '{user_type}' user",
            func=benchmark_func,
            runs=runs,
        )

    def _run_benchmark(self, *, title: str, func: t.Callable[[], t.Any], runs: int) -> None:
        """A generic function to run a benchmark and print the results.

        Args:
            title: A descriptive title for the benchmark being run.
            func: A zero-argument function that executes the code to be measured.
            runs: The number of times to execute the function.
        """
        self.stdout.write("\n" + self.style.HTTP_INFO(f"--- Running Benchmark: {title} ---"))

        timings = []
        query_counts = []

        # Warm-up run to cache any initial Django setups
        self.stdout.write("Performing one warm-up run...")
        func()

        self.stdout.write(f"Executing {runs} measured runs...")
        for i in range(runs):
            reset_queries()

            start_time = time.perf_counter()
            # The function call we are measuring
            result_count = len(func())
            end_time = time.perf_counter()

            duration = end_time - start_time
            num_queries = len(connection.queries)

            timings.append(duration)
            query_counts.append(num_queries)

            # Optional: print per-run stats
            # self.stdout.write(f"  Run {i+1}/{runs}: {duration:.4f}s, {num_queries} queries")

        total_time = sum(timings)
        avg_time = total_time / runs
        min_time = min(timings)
        max_time = max(timings)
        avg_queries = sum(query_counts) / len(query_counts)

        self.stdout.write(self.style.SUCCESS("\n--- Benchmark Results ---"))
        self.stdout.write(f"Title: {title}")
        self.stdout.write(f"Number of runs: {runs}")
        self.stdout.write(f"Objects returned per run: {result_count}")
        self.stdout.write("-" * 25)
        self.stdout.write(f"Total Time:     {total_time:.4f}s")
        self.stdout.write(self.style.WARNING(f"Average Time:   {avg_time:.4f}s"))
        self.stdout.write(f"Fastest Run:    {min_time:.4f}s")
        self.stdout.write(f"Slowest Run:    {max_time:.4f}s")
        self.stdout.write(f"Average Queries: {avg_queries:.1f}")
        self.stdout.write(self.style.SUCCESS("--- End of Results ---\n"))
