from decouple import Csv, config

from .base import REDIS_HOST, REDIS_PORT

TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN")
TELEGRAM_FSM_REDIS_URL = config("TELEGRAM_FSM_REDIS_URL", default="redis://localhost:6379/1")
TELEGRAM_SUPERUSER_IDS = [int(tg_id) for tg_id in config("TELEGRAM_SUPERUSER_IDS", cast=Csv(), default="")]
TELEGRAM_STAFF_IDS = [int(tg_id) for tg_id in config("TELEGRAM_STAFF_IDS", cast=Csv(), default="")]
TELEGRAM_OTP_EXPIRATION_MINUTES = config("TELEGRAM_OTP_EXPIRATION_MINUTES", default=15, cast=int)
AIOGRAM_REDIS_DB = config("AIOGRAM_REDIS_DB", default=1, cast=int)
AIOGRAM_REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{AIOGRAM_REDIS_DB}"
