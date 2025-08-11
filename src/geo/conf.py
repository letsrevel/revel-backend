from django.conf import settings

GEO_DATA_DIR = settings.BASE_DIR / "geo" / "data"

IP2LOCATION_DB_PATH = GEO_DATA_DIR / "IP2LOCATION-LITE-DB5.BIN"
WORLDCITIES_CSV_PATH = GEO_DATA_DIR / "worldcities.csv"
