"""Benchmark utilities package for profiling N+1 queries and performance.

This package provides shared utilities for creating benchmark commands:
- BenchmarkResult: Dataclass for capturing timing and query statistics
- BenchmarkScenario: Dataclass for defining test scenarios with pre-loaded data
- BaseBenchmarkCommand: Base class for benchmark management commands
- Query analysis utilities for detecting N+1 patterns

Benchmark classes:
- VisibilityBenchmark: Profile visibility flag building (P0 N+1 issue)
- DashboardBenchmark: Profile dashboard endpoints (P1 N+1 issues)
- NotificationsBenchmark: Profile notification dispatch (P3 N+1 issues)
- CheckoutBenchmark: Profile checkout endpoint and eligibility
"""

from .base import BaseBenchmarkCommand, BenchmarkResult, BenchmarkScenario
from .checkout import CheckoutBenchmark
from .dashboard import DashboardBenchmark
from .notifications import NotificationsBenchmark
from .query_utils import QueryBreakdown, analyze_queries, format_query_breakdown
from .visibility import VisibilityBenchmark

__all__ = [
    # Base classes
    "BaseBenchmarkCommand",
    "BenchmarkResult",
    "BenchmarkScenario",
    # Query utilities
    "QueryBreakdown",
    "analyze_queries",
    "format_query_breakdown",
    # Benchmark classes
    "VisibilityBenchmark",
    "DashboardBenchmark",
    "NotificationsBenchmark",
    "CheckoutBenchmark",
]
