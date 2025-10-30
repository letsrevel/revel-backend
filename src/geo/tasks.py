import requests
from celery import shared_task

from .conf import IP2LOCATION_DB_PATH, IP2LOCATION_TOKEN

DB_CODE = "DB5LITEBINIPV6"


@shared_task
def download_ip2location() -> None:
    """Download a fresh IP2Location database.

    Downloads the database to a temporary file and atomically moves it to the
    final location to avoid disruptions during download.

    Note: Uses the same directory as the destination for the temporary file
    to ensure writability in Docker environments and guarantee atomic move
    (same filesystem).
    """
    # Create temporary file in the same directory as destination (guaranteed writable)
    tmp_path = IP2LOCATION_DB_PATH.with_suffix(".bin.tmp")

    # Stream download to avoid loading entire file in memory
    response = requests.get(
        "https://www.ip2location.com/download/",
        params={"token": IP2LOCATION_TOKEN, "file": DB_CODE},
        stream=True,
        timeout=300,
    )
    response.raise_for_status()

    # Write to temporary file
    with tmp_path.open("wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    # Atomically replace the database file
    tmp_path.replace(IP2LOCATION_DB_PATH)
