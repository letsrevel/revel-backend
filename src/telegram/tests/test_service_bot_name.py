# src/telegram/tests/test_service_bot_name.py
"""Tests for telegram.service.get_bot_name."""

import typing as t
from unittest.mock import AsyncMock, MagicMock

import pytest
from django.core.cache import cache

from telegram import service

CACHE_KEY = "telegram:bot_name"


@pytest.fixture
def mock_bot(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A fake aiogram Bot whose get_me returns a user and whose session tracks close()."""
    bot = MagicMock()
    bot.get_me = AsyncMock(return_value=MagicMock(username="revel_test_bot"))
    bot.session.close = AsyncMock()
    monkeypatch.setattr(service, "get_bot", lambda: bot)
    return bot


@pytest.fixture(autouse=True)
def clear_bot_name_cache() -> t.Iterator[None]:
    """Isolate the bot-name cache key between tests."""
    cache.delete(CACHE_KEY)
    yield
    cache.delete(CACHE_KEY)


def test_get_bot_name_fetches_and_closes_session(mock_bot: MagicMock) -> None:
    assert service.get_bot_name() == "revel_test_bot"
    mock_bot.get_me.assert_awaited_once()
    mock_bot.session.close.assert_awaited_once()


def test_get_bot_name_closes_session_when_get_me_fails(mock_bot: MagicMock) -> None:
    mock_bot.get_me = AsyncMock(side_effect=RuntimeError("telegram down"))
    with pytest.raises(RuntimeError, match="telegram down"):
        service.get_bot_name()
    mock_bot.session.close.assert_awaited_once()
    assert cache.get(CACHE_KEY) is None


def test_get_bot_name_uses_cache_on_second_call(mock_bot: MagicMock) -> None:
    assert service.get_bot_name() == "revel_test_bot"
    assert service.get_bot_name() == "revel_test_bot"
    mock_bot.get_me.assert_awaited_once()
    assert cache.get(CACHE_KEY) == "revel_test_bot"


def test_get_bot_name_empty_username_falls_back_to_empty_string(mock_bot: MagicMock) -> None:
    mock_bot.get_me = AsyncMock(return_value=MagicMock(username=None))
    assert service.get_bot_name() == ""
