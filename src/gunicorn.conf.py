"""Gunicorn configuration, auto-loaded from the working directory.

Gunicorn reads ``./gunicorn.conf.py`` from its working directory when no
``-c`` flag is given. Production runs gunicorn from ``/app/src`` (the image's
``WORKDIR``) and ``make run-e2e`` runs it from ``src/``, so this file applies
to both without any change to the launch command.

Its only job is Prometheus multiprocess hygiene (#757). Both hooks are no-ops
unless ``PROMETHEUS_MULTIPROC_DIR`` is set, so behaviour is unchanged until
the deployment half (letsrevel/infra#35) sets the env var and mounts a
writable shared directory for the metric files.
"""

import glob
import os
import typing as t


def on_starting(server: t.Any) -> None:
    """Wipe stale metric files before any worker starts.

    ``prometheus_client`` requires the multiprocess directory to be emptied
    whenever the gunicorn master (re)starts: leftover mmap files from a
    previous run would otherwise be merged into every scrape forever.

    Args:
        server: The gunicorn Arbiter instance (unused).
    """
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not multiproc_dir:
        return
    for path in glob.glob(os.path.join(multiproc_dir, "*.db")):
        os.remove(path)


def child_exit(server: t.Any, worker: t.Any) -> None:
    """Clean up a dead worker's live-gauge metric files.

    ``multiprocess.mark_process_dead`` removes only ``gauge_live*_<pid>.db``
    files; counter/histogram files persist by design so totals never go
    backwards. Relevant here because ``--max-requests`` recycles workers
    routinely.

    Args:
        server: The gunicorn Arbiter instance (unused).
        worker: The exited worker; only ``worker.pid`` is used.
    """
    if not os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        return
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid)  # type: ignore[no-untyped-call]
