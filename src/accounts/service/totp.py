"""The one second-factor check: TOTP verification behind a per-account failure lock (#1429).

Every place that checks a TOTP code (login step 2, 2FA enable, 2FA disable) calls
:func:`verify_totp`; nothing else may call ``pyotp`` directly. Per-IP and per-user request throttles
do not stop a distributed guesser, so the account itself locks: after :data:`FAILURE_THRESHOLD`
wrong codes (counted when the attempt is reserved) verification is refused for :data:`LOCK_SECONDS`,
even for the right code, and the owner is emailed once. A success resets the counter.

Any future second factor (e.g. recovery codes) must go through this helper so it shares the counter.
State lives in the default cache (no migration). This is a security control, so it is deliberately
not affected by ``DISABLE_THROTTLING``, and an unreachable cache surfaces as a 500 (fails closed).
"""

import math
import time
import typing as t

import pyotp
import structlog
from django.core.cache import cache
from django.db import transaction

from accounts import tasks
from accounts.exceptions import TOTPLockedError
from accounts.models import RevelUser

logger = structlog.get_logger(__name__)

FAILURE_THRESHOLD = 5
LOCK_SECONDS = 15 * 60  # the lock email says "15 minutes" (totp_locked_body); keep them in step
# ponytail: the failure window is bounded by the cache TTL (a counter evicted or flushed with the
# cache restarts at zero). Upgrade path: a DB column on the user if the lock must survive a flush.
FAILURE_WINDOW_SECONDS = 24 * 60 * 60
_RESERVE_RETRIES = 3


def _fail_key(user: RevelUser) -> str:
    return f"totp:fail:{user.pk}"


def _lock_key(user: RevelUser) -> str:
    return f"totp:lock:{user.pk}"


def _raise_locked(unlock_at: float) -> t.NoReturn:
    raise TOTPLockedError(retry_after=min(LOCK_SECONDS, max(1, math.ceil(unlock_at - time.time()))))


def verify_totp(user: RevelUser, code: str, *, reason: str) -> bool:
    """Check ``code`` against the user's TOTP secret, counting failures toward a lock.

    Call it outside any ``transaction.atomic`` block that a :class:`TOTPLockedError` would roll
    back, so the failure bookkeeping (and the ``on_commit`` lock email) survives the refusal.

    Args:
        user: The account whose second factor is checked.
        code: The submitted TOTP code.
        reason: Where the check happened (``login``, ``enable``, ``disable``), for the lock log line.

    Returns:
        Whether the code is valid. ``False`` for the first four wrong codes.

    Raises:
        TOTPLockedError: The account is locked, or this wrong code reached the threshold.
    """
    if (unlock_at := cache.get(_lock_key(user))) is not None:
        _raise_locked(unlock_at)

    # Reserve the attempt *before* verifying: the counter is the budget, so however many requests are
    # in flight at once, at most FAILURE_THRESHOLD of them ever reach pyotp inside one window.
    fail_key = _fail_key(user)
    attempt = _reserve_attempt(fail_key)

    # Every locked exit clears the counter, so the next window starts fresh once the lock expires.
    if attempt > FAILURE_THRESHOLD:  # the budget went to concurrent requests: refuse unverified
        unlock_at = _ensure_locked(user, reason)
        cache.delete(fail_key)
        _raise_locked(unlock_at)
    # A lock set after our first read (by the 5th attempt, which then reset the counter) still applies.
    if (unlock_at := cache.get(_lock_key(user))) is not None:
        cache.delete(fail_key)
        _raise_locked(unlock_at)

    if pyotp.TOTP(user.totp_secret).verify(code):
        cache.delete(fail_key)
        return True
    if attempt < FAILURE_THRESHOLD:
        return False

    unlock_at = _ensure_locked(user, reason)
    cache.delete(fail_key)
    _raise_locked(unlock_at)


def _reserve_attempt(fail_key: str) -> int:
    """Atomically take the next attempt number from the failure counter.

    ``incr`` raises ``ValueError`` when the key vanished between ``add`` and ``incr`` (a success or a lock
    deleted it, or it expired). Retrying ``add`` + ``incr`` never overwrites reservations that concurrent
    requests made meanwhile, unlike a plain ``set``.

    Raises:
        RuntimeError: The key kept vanishing; fail closed (500) rather than verify uncounted.
    """
    for _ in range(_RESERVE_RETRIES):
        cache.add(fail_key, 0, timeout=FAILURE_WINDOW_SECONDS)
        try:
            return int(cache.incr(fail_key))
        except ValueError:
            continue
    raise RuntimeError("Could not reserve a two-factor attempt.")


def _ensure_locked(user: RevelUser, reason: str) -> float:
    """Create the lock if absent (the winner logs and emails the owner) and return its unlock time."""
    unlock_at = time.time() + LOCK_SECONDS
    # First writer wins: concurrent threshold-crossing attempts send one email, not several.
    if cache.add(_lock_key(user), unlock_at, timeout=LOCK_SECONDS):
        logger.warning("totp_verification_locked", user_id=str(user.id), reason=reason)
        email, language = user.email, user.language
        transaction.on_commit(
            lambda: tasks.send_account_email.delay(
                tasks.AccountEmail.TOTP_LOCKED, email, context={"language": language}
            )
        )
    return t.cast(float, cache.get(_lock_key(user), unlock_at))
