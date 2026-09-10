# src/telegram/tests/test_bot.py
import typing as t

from aiogram.fsm.storage.redis import RedisStorage

from telegram.bot import _get_redis_storage


class TestRedisStorage:
    def test_fsm_keys_get_a_ttl(self, settings: t.Any) -> None:
        """FSM state/data keys must expire.

        Redis runs with ``maxmemory-policy volatile-lru``, which can only evict keys that
        carry a TTL; TTL-less FSM keys would accumulate forever and eventually wedge Redis.
        """
        settings.AIOGRAM_FSM_TTL_SECONDS = 3600

        storage = _get_redis_storage()

        assert isinstance(storage, RedisStorage)
        assert storage.state_ttl == 3600
        assert storage.data_ttl == 3600
