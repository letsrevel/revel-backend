import threading

import structlog
from django.contrib.gis.geos import Point
from IP2Location import IP2Location

from geo import conf

logger = structlog.get_logger(__name__)

# In the default FILE_IO mode, IP2Location reads records through a single shared
# file cursor (seek + read), which is not thread-safe. Under Gunicorn's gthread
# workers, concurrent requests on one shared instance corrupt each other's reads.
# We therefore keep one handle per thread. The database pages stay in the shared
# OS page cache (loaded once), so this costs only a cheap file descriptor per
# thread — not a copy of the ~168 MB database. See issue #637.
_local = threading.local()


def get_ip2location() -> IP2Location:
    """Return a thread-local IP2Location database handle.

    Each thread gets its own file descriptor because FILE_IO mode is not
    thread-safe. Uses the database file's modification time to detect when a new
    database has been downloaded, reloading the calling thread's handle.
    """
    # Get current file modification time
    current_mtime = conf.IP2LOCATION_DB_PATH.stat().st_mtime

    # Reload if this thread hasn't loaded the database or the file has changed
    db: IP2Location | None = getattr(_local, "db", None)
    if db is None or _local.mtime != current_mtime:
        db = IP2Location(conf.IP2LOCATION_DB_PATH)
        _local.db = db
        _local.mtime = current_mtime

    return db


def resolve_ip_to_point(ip: str) -> Point | None:
    """Resolves an IP address to a geographical point."""
    ipdb = get_ip2location()
    try:
        record = ipdb.get_all(ip)
        if record is None or record.city == "-":
            return None
        return Point(float(record.longitude), float(record.latitude), srid=4326)
    except Exception:
        # warning, not debug: a broken database otherwise fails every lookup
        # invisibly (a corrupt .BIN disabled nearest-first sorting in
        # production for months — the downloader saved the ZIP as the .BIN).
        logger.warning("ip_resolution_failed", ip=ip, exc_info=True)
        return None


class LazyGeoPoint:
    """A lazy-loading geographical point for a given IP address."""

    def __init__(self, ip: str) -> None:
        """Initializes the LazyGeoPoint with an IP address."""
        self.ip = ip
        self._resolved: Point | None = None
        self._called = False

    def get(self) -> Point | None:
        """Resolves and returns the geographical point."""
        if not self._called:
            self._resolved = resolve_ip_to_point(self.ip)
            self._called = True
        return self._resolved

    def __bool__(self) -> bool:
        """Returns True if the geographical point can be resolved."""
        return self.get() is not None
