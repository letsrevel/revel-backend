import shutil
import zipfile

import requests
import structlog
from celery import shared_task
from IP2Location import IP2Location

from .conf import IP2LOCATION_DB_PATH, IP2LOCATION_TOKEN

logger = structlog.get_logger(__name__)

DB_CODE = "DB5LITEBINIPV6"
# A lookup that must succeed against a healthy database (Google DNS → US).
VALIDATION_IP = "8.8.8.8"


@shared_task(name="geo.tasks.download_ip2location")
def download_ip2location() -> None:
    """Download a fresh IP2Location database.

    The download endpoint delivers a ZIP archive containing the ``.BIN``
    database (plus license/readme text files) — saving the response body
    as-is silently breaks every geolocation lookup. Stream the archive to a
    temporary file, extract the single ``.BIN`` member, validate it with a
    real lookup, and only then atomically replace the live database.

    Note: Uses the same directory as the destination for the temporary files
    to ensure writability in Docker environments and guarantee atomic move
    (same filesystem).
    """
    zip_tmp = IP2LOCATION_DB_PATH.with_suffix(".zip.tmp")
    bin_tmp = IP2LOCATION_DB_PATH.with_suffix(".bin.tmp")

    try:
        # Stream download to avoid loading the archive in memory; the context
        # manager releases the connection even if streaming fails midway.
        with requests.get(
            "https://www.ip2location.com/download/",
            params={"token": IP2LOCATION_TOKEN, "file": DB_CODE},
            stream=True,
            timeout=300,
        ) as response:
            response.raise_for_status()
            with zip_tmp.open("wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

        with zipfile.ZipFile(zip_tmp) as archive:
            bin_members = [name for name in archive.namelist() if name.upper().endswith(".BIN")]
            if len(bin_members) != 1:
                raise ValueError(f"Expected exactly one .BIN member in the IP2Location archive, got: {bin_members}")
            with archive.open(bin_members[0]) as src, bin_tmp.open("wb") as dst:
                shutil.copyfileobj(src, dst)

        # Validate before swapping: a broken download must never replace a
        # working database. A failed lookup raises, leaving the live file
        # untouched and failing the task loudly.
        record = IP2Location(str(bin_tmp)).get_all(VALIDATION_IP)
        if record is None or not record.country_short:
            raise ValueError("Downloaded IP2Location database failed the validation lookup.")

        # Atomically replace the database file
        bin_tmp.replace(IP2LOCATION_DB_PATH)
        logger.info("ip2location_db_updated", member=bin_members[0], size=IP2LOCATION_DB_PATH.stat().st_size)
    finally:
        zip_tmp.unlink(missing_ok=True)
        bin_tmp.unlink(missing_ok=True)
