from django.contrib.gis.geos import Point
from IP2Location import IP2Location

from geo import conf

_DB: IP2Location | None = None


def get_ip2location() -> IP2Location:
    """Initializes and returns the IP2Location database object."""
    global _DB
    if _DB is None:
        _DB = IP2Location(conf.IP2LOCATION_DB_PATH)
    return _DB


def resolve_ip_to_point(ip: str) -> Point | None:
    """Resolves an IP address to a geographical point."""
    ipdb = get_ip2location()
    try:
        record = ipdb.get_all(ip)
        if record is None or record.city == "-":
            return None
        return Point(float(record.longitude), float(record.latitude), srid=4326)
    except Exception:
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
