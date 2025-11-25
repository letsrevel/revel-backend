"""Pyroscope continuous profiling setup via Grafana Alloy eBPF.

Continuous profiling is handled by Grafana Alloy using eBPF (Extended Berkeley Packet Filter).
This approach requires ZERO code changes - profiling happens at the kernel level.

Architecture:
1. Grafana Alloy runs as a privileged container with eBPF enabled
2. It discovers Python processes via Docker labels/container names
3. Profiles are collected at ~97 Hz (samples/second) with ~1% overhead
4. Data is sent to Pyroscope for storage and visualization in Grafana

Platform Support:
- Linux: ✅ Full support (eBPF works)
- macOS: ❌ Not supported (Docker Desktop doesn't support eBPF)
- Windows: ❌ Not supported

Configuration:
- observability/alloy-config.alloy: eBPF profiling configuration
- docker-compose.yaml: Pyroscope + Alloy services (production)
- docker-compose-dev.yml: Profiling disabled by default (macOS)

Manual Profiling (development alternative):
- Use py-spy: `sudo .venv/bin/py-spy record -o flamegraph.svg --pid <PID>`

See OBSERVABILITY_SPEC.md and observability/PROFILING_SETUP.md for details.
"""

import structlog
from django.conf import settings

logger = structlog.get_logger(__name__)


def init_profiling() -> None:
    """Initialize Pyroscope continuous profiling.

    Profiling is handled externally by Grafana Alloy (eBPF) - no Python code needed.
    This function exists for logging and future extensibility.
    """
    if not settings.ENABLE_OBSERVABILITY:
        logger.debug("Observability disabled - profiling will not be active")
        return

    logger.info(
        "Continuous profiling is handled by Grafana Alloy (eBPF). "
        "Profiles are automatically collected from running Python processes and sent to Pyroscope. "
        "View flamegraphs at: %s",
        getattr(settings, "GRAFANA_URL", "http://localhost:3000"),
    )
