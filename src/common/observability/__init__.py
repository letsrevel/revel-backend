"""Observability utilities for Revel."""

from .profiling import init_profiling
from .tracing import init_tracing

__all__ = ["init_tracing", "init_profiling"]
