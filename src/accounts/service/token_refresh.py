"""Refresh-token rotation with a short reuse grace, and lineage logout.

Every refresh token belongs to a *lineage* (one login). The lineage id is the ``sid`` claim, or the
token's own ``jti`` for the token a login minted (which has no ``sid``). Rotation and grace stamp
``sid`` on everything they issue and record each new token as an ``OutstandingToken`` owned by the
user, so ``accounts.jwt.blacklist_user_tokens`` and ``logout`` can find every live session.

Concurrency: rotation, grace, logout and ``blacklist_user_tokens`` all take the user-row lock, so a
logout can never miss a token minted at the same moment.
"""

import typing as t

import jwt
import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja_jwt.exceptions import InvalidToken, TokenError
from ninja_jwt.schema import TokenRefreshOutputSchema
from ninja_jwt.settings import api_settings
from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from ninja_jwt.tokens import RefreshToken
from ninja_jwt.utils import datetime_from_epoch

from accounts.models import RevelUser
from accounts.service import tenant_session

logger = structlog.get_logger(__name__)

SID_CLAIM = "sid"
_JTI = t.cast(str, api_settings.JTI_CLAIM)
_USER_ID = api_settings.USER_ID_CLAIM


class _RefreshIgnoringBlacklist(RefreshToken):
    """A refresh token that is fully verified (signature, exp, aud, type) except for the blacklist."""

    def check_blacklist(self) -> None:
        """Skip the blacklist check; callers decide what a blacklisted token means."""


def _verify(raw: str) -> _RefreshIgnoringBlacklist:
    try:
        return _RefreshIgnoringBlacklist(raw)
    except TokenError as exc:
        raise InvalidToken(str(exc)) from exc


def _lock_user(user_id: t.Any) -> RevelUser | None:
    return RevelUser.objects.select_for_update().filter(pk=user_id).first()


def _lineage_id(payload: t.Mapping[str, t.Any]) -> str:
    return str(payload.get(SID_CLAIM) or payload[_JTI])


def _blacklist(token: RefreshToken) -> bool:
    """Blacklist ``token``, returning whether this call created the blacklist row."""
    outstanding, _created = OutstandingToken.objects.get_or_create(
        jti=token[_JTI],
        defaults={"token": str(token), "expires_at": datetime_from_epoch(token["exp"])},
    )
    return BlacklistedToken.objects.get_or_create(token=outstanding)[1]


def _lineage_live_jtis(user: RevelUser, sid: str) -> list[str]:
    """Return the jtis of the user's unexpired, non-blacklisted refresh tokens in lineage ``sid``."""
    rows = OutstandingToken.objects.filter(user=user, expires_at__gt=timezone.now()).exclude(
        blacklistedtoken__isnull=False
    )
    jtis: list[str] = []
    for row in rows:
        try:
            # Our own DB rows, not input: the signature was verified when they were issued.
            payload = jwt.decode(row.token, options={"verify_signature": False})
        except jwt.PyJWTError:
            continue
        if _lineage_id(payload) == sid:
            jtis.append(row.jti)
    return jtis


def _issue(source: RefreshToken, user: RevelUser | None, sid: str) -> TokenRefreshOutputSchema:
    """Mint the successor of ``source`` in lineage ``sid`` and record it as outstanding."""
    source[SID_CLAIM] = sid
    source.set_jti()
    source.set_exp()
    source.set_iat()
    OutstandingToken.objects.create(
        user=user,
        jti=source[_JTI],
        token=str(source),
        created_at=source.current_time,
        expires_at=datetime_from_epoch(source["exp"]),
    )
    # An instance, not a dict: ninja re-validates the response, and the output schema's
    # before-validator would rotate a dict a second time.
    return TokenRefreshOutputSchema.model_construct(refresh=str(source), access=str(source.access_token))


def _grace_allows(user: RevelUser | None, jti: str, sid: str) -> bool:
    """Whether a reused (already rotated) token may be exchanged once more. Has no side effects on denial."""
    grace: int = settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS
    if grace <= 0 or user is None or not user.is_active:
        return False
    try:
        if not cache.get(f"refresh_rotated:{jti}"):
            return False
        if not _lineage_live_jtis(user, sid):
            return False
        return bool(cache.add(f"refresh_grace_used:{jti}", 1, timeout=grace))
    except Exception:
        logger.warning("refresh_grace_cache_unavailable", user_id=str(user.id), jti=jti, exc_info=True)
        return False


def _rotate_locked(token: _RefreshIgnoringBlacklist) -> TokenRefreshOutputSchema:
    user = _lock_user(token[_USER_ID])
    jti = token[_JTI]
    sid = _lineage_id(token.payload)
    rejection = InvalidToken(str(_("Token is blacklisted")))

    if BlacklistedToken.objects.filter(token__jti=jti).exists():
        if not _grace_allows(user, jti, sid):
            raise rejection
        assert user is not None  # noqa: S101 - _grace_allows guarantees it
        logger.info("refresh_token_reuse_grace_used", user_id=str(user.id), jti=jti)
        return _issue(token, user, sid)

    if not _blacklist(token):
        raise rejection
    grace: int = settings.REFRESH_TOKEN_REUSE_GRACE_SECONDS
    if grace > 0:
        try:
            cache.set(f"refresh_rotated:{jti}", 1, timeout=grace)
        except Exception:
            logger.warning("refresh_rotation_marker_failed", user_id=str(token[_USER_ID]), jti=jti)
    return _issue(token, user, sid)


def rotate(raw_refresh: str) -> TokenRefreshOutputSchema:
    """Exchange a refresh token for a new pair, honouring the single-use reuse grace.

    Raises:
        InvalidToken: The token is invalid, expired, revoked, or reused outside the grace (401).
    """
    token = _verify(raw_refresh)
    if not tenant_session.is_tenant_session_valid(token.payload, check_reactivation=True):
        # Blacklist in its own transaction so the write survives the raise below.
        with transaction.atomic():
            _blacklist(token)
        raise InvalidToken
    with transaction.atomic():
        return _rotate_locked(token)


def logout(raw_refresh: str) -> None:
    """Revoke the presented refresh token and every live token of its lineage (other logins untouched).

    A stale, already-rotated token still logs its lineage out.

    Raises:
        InvalidToken: The token is invalid, expired or forged (401).
    """
    token = _verify(raw_refresh)
    with transaction.atomic():
        user = _lock_user(token[_USER_ID])
        sid = _lineage_id(token.payload)
        _blacklist(token)
        jtis = _lineage_live_jtis(user, sid) if user is not None else []
        for outstanding in OutstandingToken.objects.filter(jti__in=jtis):
            BlacklistedToken.objects.get_or_create(token=outstanding)
        logger.info("refresh_lineage_logged_out", user_id=str(token[_USER_ID]), count=len(jtis) + 1)
