"""Tests for find_reserved_token and cache plumbing."""

import pytest
from django.core.cache import cache

from events.constants.reserved_slug_tokens import RESERVED_SLUG_TOKENS_HARDCODED
from events.models import ReservedSlugToken
from events.utils.reserved_slug_tokens import (
    CACHE_KEY,
    find_reserved_token,
    get_reserved_tokens,
    invalidate_reserved_tokens_cache,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    cache.delete(CACHE_KEY)


def test_get_reserved_tokens_includes_hardcoded() -> None:
    tokens = get_reserved_tokens()
    assert "admin" in tokens
    assert "test" in tokens
    assert RESERVED_SLUG_TOKENS_HARDCODED.issubset(tokens)


def test_get_reserved_tokens_includes_db_entries() -> None:
    ReservedSlugToken.objects.create(token="customblock", reason="")
    invalidate_reserved_tokens_cache()
    assert "customblock" in get_reserved_tokens()


def test_find_reserved_token_catches_word_order_variants() -> None:
    assert find_reserved_token("Test Choir") == "test"
    assert find_reserved_token("Choir Test") == "test"
    assert find_reserved_token("MY ADMIN ORG") == "admin"


def test_find_reserved_token_normalizes_unicode() -> None:
    assert find_reserved_token("Tëst Org") == "test"


def test_find_reserved_token_allows_legit_names() -> None:
    assert find_reserved_token("Testimony Choir") is None
    assert find_reserved_token("Acoustic Events Co") is None  # 'events' is not reserved
    assert find_reserved_token("Local Tickets Group") is None  # 'tickets' is not reserved
    assert find_reserved_token("Common Ground Society") is None  # 'common' is not reserved


def test_find_reserved_token_handles_empty_input() -> None:
    assert find_reserved_token("") is None
    assert find_reserved_token("!!! ???") is None


def test_find_reserved_token_db_entry_blocks_creation() -> None:
    ReservedSlugToken.objects.create(token="zzzweird", reason="")
    invalidate_reserved_tokens_cache()
    assert find_reserved_token("My ZZZWEIRD Project") == "zzzweird"


def test_cache_invalidation_picks_up_new_token() -> None:
    # warm the cache without the entry
    assert find_reserved_token("My Fresh Org") is None
    ReservedSlugToken.objects.create(token="fresh", reason="")
    # signal automatically invalidates the cache, so the new token is picked up
    assert find_reserved_token("My Fresh Org") == "fresh"


def test_post_save_signal_invalidates_cache() -> None:
    assert find_reserved_token("My Signaled Org") is None  # warm cache without entry
    ReservedSlugToken.objects.create(token="signaled", reason="")
    # NO manual invalidation here — signal must do it
    assert find_reserved_token("My Signaled Org") == "signaled"


def test_post_delete_signal_invalidates_cache() -> None:
    entry = ReservedSlugToken.objects.create(token="willgo", reason="")
    invalidate_reserved_tokens_cache()
    assert find_reserved_token("My willgo Org") == "willgo"
    entry.delete()
    # NO manual invalidation — signal must do it
    assert find_reserved_token("My willgo Org") is None
