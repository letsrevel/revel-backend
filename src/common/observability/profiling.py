"""Pyroscope continuous profiling setup.

DISABLED: Pyroscope profiling is currently disabled due to SDK incompatibility.

The pyroscope-io Python SDK (v0.8.11) is incompatible with Grafana Pyroscope server v1.6+.
The legacy SDK uses a different protocol than the new Grafana Pyroscope architecture (v1.0+).

Alternative options for profiling:
1. Use py-spy manually: `sudo .venv/bin/py-spy record -o flamegraph.svg --pid <PID>`
2. Wait for Grafana to release an updated Python SDK
3. Downgrade Pyroscope server to pre-1.0 version (pyroscope/pyroscope:0.37.2)

See OBSERVABILITY_SPEC.md for more details.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def init_profiling() -> None:
    """Initialize Pyroscope continuous profiling.

    Currently disabled - see module docstring for details.
    """
    if not settings.ENABLE_OBSERVABILITY:
        return

    logger.info(
        "Pyroscope profiling is DISABLED (SDK incompatibility with Grafana Pyroscope 1.6+). "
        "Use py-spy manually for ad-hoc profiling."
    )
