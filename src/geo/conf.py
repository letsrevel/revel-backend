from pathlib import Path

from django.conf import settings

GEO_DATA_DIR = settings.BASE_DIR / "geo" / "data"

IP2LOCATION_DB_PATH = GEO_DATA_DIR / "IP2LOCATION-LITE-DB5.BIN"
WORLDCITIES_CSV_PATH = GEO_DATA_DIR / "worldcities.csv"
WORLDCITIES_MINI_CSV_PATH = GEO_DATA_DIR / "worldcities.mini.csv"
IP2LOCATION_TOKEN = getattr(settings, "IP2LOCATION_TOKEN", None)


def resolve_worldcities_csv() -> Path:
    """Return the full worldcities CSV if present, else the tracked mini fallback.

    The full ``worldcities.csv`` is gitignored and absent from a fresh clone and
    the published image. Falling back to ``worldcities.mini.csv`` (50 cities) keeps
    the ``0002_load_cities`` migration from crash-looping the web container.

    Returns:
        Path to the worldcities CSV that should be loaded.
    """
    return WORLDCITIES_CSV_PATH if WORLDCITIES_CSV_PATH.exists() else WORLDCITIES_MINI_CSV_PATH
