#!/usr/bin/env python
# mypy: ignore-errors
# ruff: noqa
"""CLI tool to debug Silk profiling data.

Usage:
    python scripts/silk_debug.py <request_id>
    python scripts/silk_debug.py <request_id> --duplicates
    python scripts/silk_debug.py <request_id> --slow
    python scripts/silk_debug.py <request_id> --full
"""

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import django

# Setup Django
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "revel.settings")
django.setup()

import sqlparse  # noqa: E402
from silk.models import Request, SQLQuery  # noqa: E402


def format_sql(sql: str, truncate: int | None = None) -> str:
    """Format SQL for readable output."""
    formatted = sqlparse.format(sql, reindent=True, keyword_case="upper")
    if truncate and len(formatted) > truncate:
        return formatted[:truncate] + "..."
    return formatted


def normalize_sql(sql: str) -> str:
    """Normalize SQL for duplicate detection (remove specific values)."""
    import re

    # Remove specific UUIDs
    sql = re.sub(r"'[0-9a-f-]{36}'", "'<UUID>'", sql)
    # Remove specific integers in IN clauses
    sql = re.sub(r"\bIN \([0-9, ]+\)", "IN (<IDS>)", sql)
    # Remove specific string values
    sql = re.sub(r"'[^']*'", "'<STR>'", sql)
    # Remove specific numbers
    sql = re.sub(r"\b\d+\b", "<NUM>", sql)
    return sql


def get_request_info(request_id: str) -> Request:
    """Print basic request information."""
    try:
        req = Request.objects.get(id=request_id)
    except Request.DoesNotExist:
        print(f"❌ Request {request_id} not found")
        sys.exit(1)

    print("=" * 80)
    print("REQUEST INFO")
    print("=" * 80)
    print(f"ID:           {req.id}")
    print(f"Path:         {req.path}")
    print(f"Method:       {req.method}")
    try:
        print(f"Status:       {req.response.status_code}")
    except Exception:
        print("Status:       N/A")
    print(f"Time:         {req.time_taken:.2f}ms" if req.time_taken else "Time:         N/A")
    print(f"Num queries:  {req.num_sql_queries}")
    db_time = req.time_spent_on_sql_queries
    print(f"DB time:      {db_time:.2f}ms" if db_time else "DB time:      N/A")
    print(f"Start:        {req.start_time}")
    print()
    return req


def show_queries(request_id: str, show_sql: bool = False) -> None:
    """List all queries for a request."""
    queries = SQLQuery.objects.filter(request_id=request_id).order_by("start_time")

    print("=" * 80)
    print(f"ALL QUERIES ({queries.count()} total)")
    print("=" * 80)

    for i, q in enumerate(queries, 1):
        tables = ", ".join(q.tables_involved) if q.tables_involved else "?"
        time_ms = q.time_taken or 0
        print(f"{i:3}. [{time_ms:6.2f}ms] {tables[:50]:<50}")
        if show_sql:
            print(f"     {format_sql(q.query, truncate=200)}")
            print()


def show_duplicates(request_id: str, show_traceback: bool = False) -> None:
    """Find and show duplicate/similar queries (N+1 detection)."""
    queries = SQLQuery.objects.filter(request_id=request_id)

    # Group by normalized SQL
    normalized_counts: Counter[str] = Counter()
    examples: dict[str, str] = {}
    tracebacks: dict[str, list[str]] = {}

    for q in queries:
        norm = normalize_sql(q.query)
        normalized_counts[norm] += 1
        if norm not in examples:
            examples[norm] = q.query
            tracebacks[norm] = []
        if q.traceback_ln_only:
            tracebacks[norm].append(q.traceback_ln_only)

    duplicates = [(sql, count) for sql, count in normalized_counts.items() if count > 1]
    duplicates.sort(key=lambda x: x[1], reverse=True)

    print("=" * 80)
    print("DUPLICATE/SIMILAR QUERIES (N+1 candidates)")
    print("=" * 80)

    if not duplicates:
        print("✅ No duplicate queries detected!")
        return

    for norm_sql, count in duplicates:
        print(f"\n🔴 {count}x similar queries:")
        print("-" * 40)
        print(format_sql(examples[norm_sql], truncate=500))

        if show_traceback and tracebacks.get(norm_sql):
            print("\n📍 Call locations:")
            for i, tb in enumerate(tracebacks[norm_sql][:3], 1):  # Show first 3 tracebacks
                # Extract only application code lines (skip site-packages, threading, etc)
                lines = []
                for line in tb.split("\n"):
                    if not line.strip():
                        continue
                    # Skip library/framework code
                    if any(
                        skip in line
                        for skip in ["site-packages", "threading.py", "socketserver.py", "/lib/python", "silk/sql.py"]
                    ):
                        continue
                    lines.append(line)
                if lines:
                    print(f"\n  #{i}:")
                    for line in lines[-6:]:  # Last 6 app code lines
                        print(f"    {line.strip()}")
        print()


def show_slow_queries(request_id: str, threshold_ms: float = 5.0) -> None:
    """Show queries slower than threshold."""
    queries = SQLQuery.objects.filter(request_id=request_id, time_taken__gte=threshold_ms).order_by("-time_taken")

    print("=" * 80)
    print(f"SLOW QUERIES (>{threshold_ms}ms)")
    print("=" * 80)

    if not queries.exists():
        print(f"✅ No queries slower than {threshold_ms}ms")
        return

    for q in queries:
        tables = ", ".join(q.tables_involved) if q.tables_involved else "?"
        print(f"\n⏱️  {q.time_taken:.2f}ms - Tables: {tables}")
        print("-" * 40)
        print(format_sql(q.query, truncate=800))


def show_by_table(request_id: str) -> None:
    """Group queries by table."""
    queries = SQLQuery.objects.filter(request_id=request_id)

    table_stats: dict[str, dict[str, float | int]] = {}

    for q in queries:
        tables = q.tables_involved if q.tables_involved else ["unknown"]
        for table in tables:
            if table not in table_stats:
                table_stats[table] = {"count": 0, "time": 0.0}
            table_stats[table]["count"] += 1
            table_stats[table]["time"] += q.time_taken or 0

    print("=" * 80)
    print("QUERIES BY TABLE")
    print("=" * 80)
    print(f"{'Table':<40} {'Count':>8} {'Time (ms)':>12}")
    print("-" * 62)

    for table, stats in sorted(table_stats.items(), key=lambda x: x[1]["count"], reverse=True):
        print(f"{table:<40} {stats['count']:>8} {stats['time']:>12.2f}")


def show_pyprofile(request_id: str, top_n: int = 30) -> None:
    """Show Python cProfile data if available."""
    req = Request.objects.get(id=request_id)

    print("=" * 80)
    print("PYTHON PROFILER (cProfile)")
    print("=" * 80)

    if not req.pyprofile:
        print("❌ No profiler data available. Enable SILKY_PYTHON_PROFILER=True")
        return

    # Parse and show the profile
    lines = req.pyprofile.strip().split("\n")

    # Show header
    for line in lines[:4]:
        print(line)

    print()
    print("Top functions by cumulative time:")
    print("-" * 80)

    # Filter to interesting lines (skip header, show app code prominently)
    for line in lines[4 : 4 + top_n]:
        # Highlight app code
        if "revel-backend/src/" in line:
            print(f"🔥 {line}")
        elif "site-packages" not in line and line.strip():
            print(f"   {line}")
        else:
            print(f"   {line}")


def show_timeline(request_id: str) -> None:
    """Show query execution timeline."""
    queries = SQLQuery.objects.filter(request_id=request_id).order_by("start_time")

    print("=" * 80)
    print("QUERY TIMELINE")
    print("=" * 80)

    if not queries.exists():
        print("No queries found")
        return

    first_query = queries.first()
    if not first_query or not first_query.start_time:
        print("No timing data available")
        return

    first_start = first_query.start_time
    for i, q in enumerate(queries, 1):
        if not q.start_time:
            continue
        offset = (q.start_time - first_start).total_seconds() * 1000
        tables = q.tables_involved[0] if q.tables_involved else "?"
        time_taken = q.time_taken or 0
        bar_len = int(time_taken / 2)
        bar = "█" * min(bar_len, 30)
        print(f"{i:3}. +{offset:7.1f}ms [{time_taken:5.1f}ms] {bar} {tables}")


def list_requests(
    limit: int = 20,
    path_filter: str | None = None,
    method_filter: str | None = None,
    status_filter: int | None = None,
    min_queries: int | None = None,
    min_time: float | None = None,
    min_db_time: float | None = None,
    sort_by: str = "time",
    ascending: bool = False,
) -> None:
    """List Silk requests with filtering and sorting options."""
    qs = Request.objects.all()

    # Apply filters (DB-level)
    if path_filter:
        qs = qs.filter(path__icontains=path_filter)
    if method_filter:
        qs = qs.filter(method__iexact=method_filter)
    if min_queries is not None:
        qs = qs.filter(num_sql_queries__gte=min_queries)
    if min_time is not None:
        qs = qs.filter(time_taken__gte=min_time)

    # Apply sorting (db_time requires post-processing since it's a computed property)
    sort_field_map = {
        "time": "start_time",
        "recent": "start_time",
        "queries": "num_sql_queries",
        "duration": "time_taken",
    }

    if sort_by == "db_time":
        # For db_time sorting, we need to fetch all and sort in Python
        requests = list(qs)
        requests.sort(key=lambda r: r.time_spent_on_sql_queries or 0, reverse=not ascending)
        requests = requests[:limit]
    else:
        sort_field = sort_field_map.get(sort_by, "start_time")
        if not ascending:
            sort_field = f"-{sort_field}"
        requests = list(qs.order_by(sort_field)[: limit * 2])  # Fetch extra for filtering

    # Filter by min_db_time (computed property, must be done in Python)
    if min_db_time is not None:
        requests = [r for r in requests if (r.time_spent_on_sql_queries or 0) >= min_db_time]

    # Filter by status code (must be done after query due to related field)
    if status_filter is not None:
        requests = [
            r for r in requests if hasattr(r, "response") and r.response and r.response.status_code == status_filter
        ]

    # Apply limit after Python filters
    requests = requests[:limit]

    # Build title
    title_parts = [f"REQUESTS (top {limit} by {sort_by})"]
    filters = []
    if path_filter:
        filters.append(f"path~'{path_filter}'")
    if method_filter:
        filters.append(f"method={method_filter}")
    if status_filter:
        filters.append(f"status={status_filter}")
    if min_queries:
        filters.append(f"queries>={min_queries}")
    if min_time:
        filters.append(f"time>={min_time}ms")
    if min_db_time:
        filters.append(f"db_time>={min_db_time}ms")
    if filters:
        title_parts.append(f"[{', '.join(filters)}]")

    print("=" * 115)
    print(" ".join(title_parts))
    print("=" * 115)
    print(f"{'ID':<38} {'Method':<6} {'Status':<6} {'Queries':>8} {'Time':>10} {'DB Time':>10} {'Path':<30}")
    print("-" * 115)

    for req in requests:
        try:
            status = req.response.status_code
        except Exception:
            status = "?"
        time_str = f"{req.time_taken:.0f}ms" if req.time_taken else "?"
        db_time_str = f"{req.time_spent_on_sql_queries:.0f}ms" if req.time_spent_on_sql_queries else "?"
        print(
            f"{req.id}  {req.method:<6} {status:<6} {req.num_sql_queries:>8} {time_str:>10} {db_time_str:>10} {req.path[:30]:<30}"
        )


def show_stats(path_filter: str | None = None, method_filter: str | None = None) -> None:
    """Show aggregate statistics for requests."""
    from django.db.models import Avg, Count, Max, Min, Sum

    qs = Request.objects.all()
    if path_filter:
        qs = qs.filter(path__icontains=path_filter)
    if method_filter:
        qs = qs.filter(method__iexact=method_filter)

    # Basic stats from DB fields
    stats = qs.aggregate(
        count=Count("id"),
        total_queries=Sum("num_sql_queries"),
        avg_queries=Avg("num_sql_queries"),
        max_queries=Max("num_sql_queries"),
        avg_time=Avg("time_taken"),
        max_time=Max("time_taken"),
        min_time=Min("time_taken"),
    )

    # Calculate DB time stats manually (time_spent_on_sql_queries is a property)
    db_times = [r.time_spent_on_sql_queries for r in qs if r.time_spent_on_sql_queries]
    avg_db_time = sum(db_times) / len(db_times) if db_times else None
    max_db_time = max(db_times) if db_times else None
    total_db_time = sum(db_times) if db_times else None

    print("=" * 60)
    print("AGGREGATE STATISTICS")
    if path_filter or method_filter:
        filters = []
        if path_filter:
            filters.append(f"path~'{path_filter}'")
        if method_filter:
            filters.append(f"method={method_filter}")
        print(f"Filters: {', '.join(filters)}")
    print("=" * 60)
    print(f"Total requests:     {stats['count']:,}")
    print(f"Total queries:      {stats['total_queries']:,}" if stats["total_queries"] else "Total queries:      0")
    print()
    print("Query counts:")
    print(f"  Average:          {stats['avg_queries']:.1f}" if stats["avg_queries"] else "  Average:          N/A")
    print(f"  Maximum:          {stats['max_queries']}" if stats["max_queries"] else "  Maximum:          N/A")
    print()
    print("Response time:")
    print(f"  Average:          {stats['avg_time']:.1f}ms" if stats["avg_time"] else "  Average:          N/A")
    print(f"  Maximum:          {stats['max_time']:.1f}ms" if stats["max_time"] else "  Maximum:          N/A")
    print(f"  Minimum:          {stats['min_time']:.1f}ms" if stats["min_time"] else "  Minimum:          N/A")
    print()
    print("Database time:")
    print(f"  Average:          {avg_db_time:.1f}ms" if avg_db_time else "  Average:          N/A")
    print(f"  Maximum:          {max_db_time:.1f}ms" if max_db_time else "  Maximum:          N/A")
    print(f"  Total:            {total_db_time:.1f}ms" if total_db_time else "  Total:            N/A")


def show_endpoints_summary(limit: int = 20, min_count: int = 1) -> None:
    """Show summary grouped by endpoint (path + method)."""
    from collections import defaultdict

    endpoint_stats: dict[tuple[str, str], dict[str, float | int | list[float]]] = defaultdict(
        lambda: {"count": 0, "total_queries": 0, "total_time": 0.0, "total_db_time": 0.0, "times": []}
    )

    for req in Request.objects.all():
        # Normalize path by removing UUIDs and IDs
        import re

        path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{uuid}", req.path)
        path = re.sub(r"/\d+", "/{id}", path)
        key = (req.method, path)

        endpoint_stats[key]["count"] += 1
        endpoint_stats[key]["total_queries"] += req.num_sql_queries or 0
        endpoint_stats[key]["total_time"] += req.time_taken or 0
        endpoint_stats[key]["total_db_time"] += req.time_spent_on_sql_queries or 0
        if req.time_taken:
            endpoint_stats[key]["times"].append(req.time_taken)

    # Calculate averages and sort
    results = []
    for (method, path), stats in endpoint_stats.items():
        if stats["count"] < min_count:
            continue
        avg_queries = stats["total_queries"] / stats["count"] if stats["count"] else 0
        avg_time = stats["total_time"] / stats["count"] if stats["count"] else 0
        avg_db_time = stats["total_db_time"] / stats["count"] if stats["count"] else 0
        p95_time = sorted(stats["times"])[int(len(stats["times"]) * 0.95)] if stats["times"] else 0
        results.append(
            {
                "method": method,
                "path": path,
                "count": stats["count"],
                "avg_queries": avg_queries,
                "avg_time": avg_time,
                "avg_db_time": avg_db_time,
                "p95_time": p95_time,
            }
        )

    # Sort by average time descending
    results.sort(key=lambda x: x["avg_time"], reverse=True)
    results = results[:limit]

    print("=" * 130)
    print(f"ENDPOINTS SUMMARY (top {limit} by avg time, min {min_count} requests)")
    print("=" * 130)
    print(
        f"{'Method':<6} {'Count':>6} {'Avg Queries':>12} {'Avg Time':>10} {'P95 Time':>10} {'Avg DB':>10} {'Path':<50}"
    )
    print("-" * 130)

    for r in results:
        print(
            f"{r['method']:<6} {r['count']:>6} {r['avg_queries']:>12.1f} "
            f"{r['avg_time']:>9.0f}ms {r['p95_time']:>9.0f}ms {r['avg_db_time']:>9.0f}ms {r['path'][:50]:<50}"
        )


def show_slow_endpoints(threshold_ms: float = 200, limit: int = 20) -> None:
    """Show requests slower than threshold, grouped by endpoint."""
    from collections import defaultdict

    slow_requests = Request.objects.filter(time_taken__gte=threshold_ms).order_by("-time_taken")

    endpoint_slow: dict[tuple[str, str], list[Request]] = defaultdict(list)
    for req in slow_requests:
        import re

        path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "/{uuid}", req.path)
        path = re.sub(r"/\d+", "/{id}", path)
        key = (req.method, path)
        endpoint_slow[key].append(req)

    print("=" * 120)
    print(f"SLOW REQUESTS (>{threshold_ms}ms)")
    print("=" * 120)

    if not endpoint_slow:
        print(f"✅ No requests slower than {threshold_ms}ms")
        return

    # Sort by count of slow requests
    sorted_endpoints = sorted(endpoint_slow.items(), key=lambda x: len(x[1]), reverse=True)[:limit]

    for (method, path), requests in sorted_endpoints:
        times = [r.time_taken for r in requests if r.time_taken]
        queries = [r.num_sql_queries for r in requests if r.num_sql_queries]
        avg_time = sum(times) / len(times) if times else 0
        max_time = max(times) if times else 0
        avg_queries = sum(queries) / len(queries) if queries else 0

        print(f"\n🔴 {method} {path}")
        print(
            f"   {len(requests)} slow requests | Avg: {avg_time:.0f}ms | Max: {max_time:.0f}ms | Avg queries: {avg_queries:.0f}"
        )
        # Show worst 3
        for req in requests[:3]:
            print(f"   └─ {req.id}  {req.time_taken:.0f}ms  {req.num_sql_queries} queries")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Debug Silk profiling data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # List recent requests
  %(prog)s --list --sort queries        # List by most queries
  %(prog)s --list --sort duration       # List by slowest
  %(prog)s --list --min-queries 50      # Only requests with 50+ queries
  %(prog)s --stats                      # Show aggregate statistics
  %(prog)s --endpoints                  # Show endpoint summary
  %(prog)s --slow-endpoints             # Show slow endpoints grouped
  %(prog)s <request_id> --full          # Full analysis of a request
  %(prog)s <request_id> -d --traceback  # Show duplicates with tracebacks
        """,
    )
    parser.add_argument("request_id", nargs="?", help="Silk request ID (UUID)")

    # Single request analysis options
    analysis = parser.add_argument_group("Single request analysis")
    analysis.add_argument("--duplicates", "-d", action="store_true", help="Show duplicate queries (N+1 detection)")
    analysis.add_argument("--slow", "-s", action="store_true", help="Show slow queries for this request")
    analysis.add_argument("--slow-threshold", type=float, default=5.0, help="Slow query threshold in ms (default: 5)")
    analysis.add_argument("--tables", "-t", action="store_true", help="Group queries by table")
    analysis.add_argument("--timeline", action="store_true", help="Show query timeline")
    analysis.add_argument("--full", "-f", action="store_true", help="Show all analyses")
    analysis.add_argument("--sql", action="store_true", help="Show SQL for each query")
    analysis.add_argument("--traceback", "-tb", action="store_true", help="Show tracebacks for duplicate queries")
    analysis.add_argument("--profile", "-prof", action="store_true", help="Show Python cProfile data")

    # Listing options
    listing = parser.add_argument_group("Request listing")
    listing.add_argument("--list", "-l", action="store_true", help="List requests")
    listing.add_argument("--limit", type=int, default=20, help="Number of requests to show (default: 20)")
    listing.add_argument(
        "--sort",
        choices=["recent", "queries", "duration", "db_time"],
        default="recent",
        help="Sort by: recent, queries, duration, db_time (default: recent)",
    )
    listing.add_argument("--asc", action="store_true", help="Sort ascending instead of descending")

    # Filters
    filters = parser.add_argument_group("Filters (for --list, --stats, --endpoints)")
    filters.add_argument("--path", "-p", type=str, help="Filter by path (contains)")
    filters.add_argument("--method", "-m", type=str, help="Filter by HTTP method (GET, POST, etc.)")
    filters.add_argument("--status", type=int, help="Filter by status code")
    filters.add_argument("--min-queries", type=int, help="Minimum number of queries")
    filters.add_argument("--min-time", type=float, help="Minimum response time in ms")
    filters.add_argument("--min-db-time", type=float, help="Minimum DB time in ms")

    # Aggregate views
    aggregate = parser.add_argument_group("Aggregate views")
    aggregate.add_argument("--stats", action="store_true", help="Show aggregate statistics")
    aggregate.add_argument("--endpoints", action="store_true", help="Show endpoints summary (grouped by path pattern)")
    aggregate.add_argument("--slow-endpoints", action="store_true", help="Show slow endpoints grouped")
    aggregate.add_argument(
        "--slow-endpoint-threshold", type=float, default=200.0, help="Threshold for slow endpoints in ms (default: 200)"
    )
    aggregate.add_argument("--min-count", type=int, default=1, help="Minimum request count for endpoints (default: 1)")

    args = parser.parse_args()

    # Aggregate views
    if args.stats:
        show_stats(path_filter=args.path, method_filter=args.method)
        return

    if args.endpoints:
        show_endpoints_summary(limit=args.limit, min_count=args.min_count)
        return

    if args.slow_endpoints:
        show_slow_endpoints(threshold_ms=args.slow_endpoint_threshold, limit=args.limit)
        return

    # List mode
    if args.list or (
        not args.request_id and not any([args.duplicates, args.slow, args.tables, args.timeline, args.full])
    ):
        list_requests(
            limit=args.limit,
            path_filter=args.path,
            method_filter=args.method,
            status_filter=args.status,
            min_queries=args.min_queries,
            min_time=args.min_time,
            min_db_time=args.min_db_time,
            sort_by=args.sort,
            ascending=args.asc,
        )
        return

    if not args.request_id:
        parser.error("request_id is required unless using --list, --stats, --endpoints, or --slow-endpoints")

    # Always show request info
    get_request_info(args.request_id)

    if args.full:
        args.duplicates = args.slow = args.tables = args.timeline = True

    if args.duplicates or args.full:
        show_duplicates(args.request_id, show_traceback=args.traceback)

    if args.slow or args.full:
        show_slow_queries(args.request_id, args.slow_threshold)

    if args.tables or args.full:
        show_by_table(args.request_id)

    if args.timeline or args.full:
        show_timeline(args.request_id)

    if args.profile or args.full:
        show_pyprofile(args.request_id)

    if not any([args.duplicates, args.slow, args.tables, args.timeline, args.full, args.profile]):
        show_queries(args.request_id, show_sql=args.sql)


if __name__ == "__main__":
    main()
