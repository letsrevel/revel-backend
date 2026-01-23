"""Query analysis utilities for detecting N+1 patterns.

This module provides tools for:
- Breaking down queries by type and table
- Detecting N+1 patterns (repeated similar queries)
- Formatting query analysis for display
"""

import re
import typing as t
from dataclasses import dataclass, field


@dataclass
class QueryBreakdown:
    """Breakdown of queries by type and table."""

    query_type: str  # SELECT, INSERT, UPDATE, DELETE
    table: str
    count: int
    total_time: float
    sample_sql: str = ""

    @property
    def avg_time_ms(self) -> float:
        """Average time per query in milliseconds."""
        return (self.total_time / self.count) * 1000 if self.count else 0


@dataclass
class N1Detection:
    """Detection result for potential N+1 queries."""

    table: str
    pattern: str
    count: int
    is_likely_n1: bool
    sample_sql: str = ""


@dataclass
class QueryAnalysis:
    """Complete analysis of a query set."""

    total_queries: int
    total_time: float
    breakdown: list[QueryBreakdown] = field(default_factory=list)
    potential_n1: list[N1Detection] = field(default_factory=list)

    @property
    def total_time_ms(self) -> float:
        """Total time in milliseconds."""
        return self.total_time * 1000


def analyze_queries(queries: list[dict[str, t.Any]]) -> QueryAnalysis:
    """Analyze a list of Django connection.queries for patterns.

    Args:
        queries: List of query dicts from django.db.connection.queries

    Returns:
        QueryAnalysis with breakdown and N+1 detection
    """
    total_time = sum(float(q.get("time", 0)) for q in queries)

    # Group by type + table
    groups: dict[str, list[dict[str, t.Any]]] = {}
    for q in queries:
        sql = q.get("sql", "")
        key = _extract_query_key(sql)
        if key not in groups:
            groups[key] = []
        groups[key].append(q)

    # Build breakdown
    breakdown: list[QueryBreakdown] = []
    for key, group_queries in groups.items():
        parts = key.split(" ", 1)
        query_type = parts[0] if parts else "OTHER"
        table = parts[1] if len(parts) > 1 else "unknown"

        breakdown.append(
            QueryBreakdown(
                query_type=query_type,
                table=table,
                count=len(group_queries),
                total_time=sum(float(q.get("time", 0)) for q in group_queries),
                sample_sql=group_queries[0].get("sql", "")[:200] if group_queries else "",
            )
        )

    # Sort by count descending
    breakdown.sort(key=lambda x: x.count, reverse=True)

    # Detect potential N+1 patterns
    potential_n1 = _detect_n1_patterns(queries)

    return QueryAnalysis(
        total_queries=len(queries),
        total_time=total_time,
        breakdown=breakdown,
        potential_n1=potential_n1,
    )


def format_query_breakdown(
    queries: list[dict[str, t.Any]],
    max_groups: int = 15,
) -> list[str]:
    """Format query breakdown for display.

    Args:
        queries: List of query dicts from django.db.connection.queries
        max_groups: Maximum number of groups to display

    Returns:
        List of formatted lines
    """
    analysis = analyze_queries(queries)
    lines: list[str] = []

    for breakdown in analysis.breakdown[:max_groups]:
        lines.append(
            f"{breakdown.query_type} {breakdown.table}: {breakdown.count} queries, {breakdown.total_time:.4f}s"
        )

    remaining = len(analysis.breakdown) - max_groups
    if remaining > 0:
        lines.append(f"... and {remaining} more query groups")

    # Add N+1 warnings
    if analysis.potential_n1:
        lines.append("")
        lines.append("POTENTIAL N+1 DETECTED:")
        for n1 in analysis.potential_n1:
            if n1.is_likely_n1:
                lines.append(f"  {n1.table}: {n1.count} similar queries (pattern: {n1.pattern})")

    return lines


def _extract_query_key(sql: str) -> str:
    """Extract a key from SQL for grouping (type + table)."""
    # Extract query type
    match = re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE)\s+", sql, re.IGNORECASE)
    query_type = match.group(1).upper() if match else "OTHER"

    # Extract table name
    if query_type == "SELECT":
        table_match = re.search(r'FROM\s+["\']?(\w+)["\']?', sql, re.IGNORECASE)
    elif query_type == "INSERT":
        table_match = re.search(r'INTO\s+["\']?(\w+)["\']?', sql, re.IGNORECASE)
    elif query_type in ("UPDATE", "DELETE"):
        table_match = re.search(r'(?:UPDATE|FROM)\s+["\']?(\w+)["\']?', sql, re.IGNORECASE)
    else:
        table_match = None

    table = table_match.group(1) if table_match else "unknown"

    return f"{query_type} {table}"


def _detect_n1_patterns(queries: list[dict[str, t.Any]]) -> list[N1Detection]:
    """Detect potential N+1 query patterns.

    N+1 patterns are characterized by:
    - Multiple similar SELECT queries to the same table
    - Usually differ only in the WHERE clause ID
    """
    detections: list[N1Detection] = []

    # Group queries by normalized pattern (remove specific IDs)
    pattern_groups: dict[str, list[dict[str, t.Any]]] = {}
    for q in queries:
        sql = q.get("sql", "")
        if not sql.upper().startswith("SELECT"):
            continue

        # Normalize: replace UUIDs and numbers with placeholders
        normalized = re.sub(
            r"'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'",
            "'<UUID>'",
            sql,
        )
        normalized = re.sub(r"\b\d+\b", "<N>", normalized)

        if normalized not in pattern_groups:
            pattern_groups[normalized] = []
        pattern_groups[normalized].append(q)

    # Check for repeated patterns (likely N+1)
    for pattern, group_queries in pattern_groups.items():
        if len(group_queries) >= 3:  # Threshold for N+1 detection
            # Extract table name
            table_match = re.search(r'FROM\s+["\']?(\w+)["\']?', pattern, re.IGNORECASE)
            table = table_match.group(1) if table_match else "unknown"

            # Truncate pattern for display
            short_pattern = pattern[:100] + "..." if len(pattern) > 100 else pattern

            detections.append(
                N1Detection(
                    table=table,
                    pattern=short_pattern,
                    count=len(group_queries),
                    is_likely_n1=len(group_queries) >= 5,  # High confidence if 5+
                    sample_sql=group_queries[0].get("sql", "")[:200],
                )
            )

    # Sort by count descending
    detections.sort(key=lambda x: x.count, reverse=True)

    return detections
