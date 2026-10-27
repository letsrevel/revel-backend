"""Server-issued signed guest identities for seat holds (spec §1: never client-minted)."""

import typing as t
import uuid

from django.core import signing
from django.http import HttpRequest

GUEST_HOLD_COOKIE = "revel_guest_hold"
# Cross-site (white-label tenant host) browsers neither store nor send the Lax cookie, so the
# frontend keeps the token first-party and sends it back in this header instead.
GUEST_HOLD_HEADER = "X-Guest-Hold"
_SALT = "events.seat-hold-guest"
_MAX_AGE_SECONDS = 60 * 60 * 24


def issue_guest_hold_token() -> tuple[str, str]:
    """Create a fresh guest hold session. Returns (session_id, signed_token)."""
    session_id = uuid.uuid4().hex
    return session_id, signing.dumps({"sid": session_id}, salt=_SALT)


def resolve_guest_session(token: str | None) -> str | None:
    """Return the session id for a valid token, else None."""
    if not token:
        return None
    try:
        payload = signing.loads(token, salt=_SALT, max_age=_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return None
    sid = payload.get("sid")
    return sid if isinstance(sid, str) and len(sid) == 32 else None


class GuestHold(t.NamedTuple):
    """A validated guest hold session: its id, the signed token that proves it, and where it came from.

    ``source`` is ``"header"`` or ``"cookie"`` when resolved from a request, ``"new"`` when minted
    by this request.
    """

    session_id: str
    token: str
    source: t.Literal["header", "cookie", "new"]


def resolve_request_guest_hold(request: HttpRequest) -> GuestHold | None:
    """Resolve the guest hold session carried by a request, if any.

    Order: the ``X-Guest-Hold`` header first (the only channel that survives a cross-site
    tenant host), then the ``revel_guest_hold`` cookie. A header or cookie that is missing,
    tampered with or expired counts as absent, so a stale header never shadows a valid cookie.
    """
    sources: tuple[tuple[t.Literal["header", "cookie"], str | None], ...] = (
        ("header", request.headers.get(GUEST_HOLD_HEADER)),
        ("cookie", request.COOKIES.get(GUEST_HOLD_COOKIE)),
    )
    for source, token in sources:
        if token and (session_id := resolve_guest_session(token)) is not None:
            return GuestHold(session_id, token, source)
    return None
