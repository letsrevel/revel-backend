"""Reserved-slug-token validator with cached union of hardcoded + DB tokens."""

import typing as t

from django.core.cache import cache
from django.utils.text import slugify

CACHE_KEY = "reserved_slug_tokens:v1"
CACHE_TTL = 300  # 5 minutes; signals invalidate immediately, TTL is the safety net.


def _load_tokens() -> frozenset[str]:
    # Imports are deferred so this module can be imported during app load
    # without triggering circular references through events.models.
    from events.constants.reserved_slug_tokens import RESERVED_SLUG_TOKENS_HARDCODED
    from events.models import ReservedSlugToken

    db_tokens = set(ReservedSlugToken.objects.values_list("token", flat=True))
    return frozenset(RESERVED_SLUG_TOKENS_HARDCODED | db_tokens)


def get_reserved_tokens() -> frozenset[str]:
    """Return the union of hardcoded + DB reserved tokens (cached)."""
    return t.cast(frozenset[str], cache.get_or_set(CACHE_KEY, _load_tokens, CACHE_TTL))


def invalidate_reserved_tokens_cache() -> None:
    """Clear the cached reserved-token set. Called from signal receivers."""
    cache.delete(CACHE_KEY)


def find_reserved_token(name: str) -> str | None:
    """Return the first reserved token found in the slugified name, or None.

    The name is slugified (lowercase, ASCII-folded, punctuation/whitespace
    collapsed to ``-``) and split on ``-``. Each non-empty segment is checked
    against the reserved set. Returns the first hit so callers can surface
    which token tripped the check.
    """
    reserved = get_reserved_tokens()
    for piece in slugify(name).split("-"):
        if piece and piece in reserved:
            return piece
    return None
