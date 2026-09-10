from decouple import config

from .base import REDIS_HOST, REDIS_PORT

TELEGRAM_BOT_TOKEN = config("TELEGRAM_BOT_TOKEN", default="0000000000:AABBCCDD")
TELEGRAM_OTP_EXPIRATION_MINUTES = config("TELEGRAM_OTP_EXPIRATION_MINUTES", default=15, cast=int)
AIOGRAM_REDIS_DB = config("AIOGRAM_REDIS_DB", default=1, cast=int)
AIOGRAM_REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{AIOGRAM_REDIS_DB}"
# Redis runs with maxmemory-policy volatile-lru, which can only evict keys that carry a TTL.
AIOGRAM_FSM_TTL_SECONDS = config("AIOGRAM_FSM_TTL_SECONDS", default=86400, cast=int)
