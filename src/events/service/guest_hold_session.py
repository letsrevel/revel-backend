"""Server-issued signed guest identities for seat holds (spec §1: never client-minted)."""

import uuid

from django.core import signing

GUEST_HOLD_COOKIE = "revel_guest_hold"
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
