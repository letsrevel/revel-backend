"""Per-account TOTP failure lock (#1429): the shared second-factor check."""

import time
import typing as t
from unittest.mock import MagicMock, patch

import pyotp
import pytest
from django.core.cache import cache

from accounts.exceptions import TOTPLockedError
from accounts.models import RevelUser
from accounts.service import totp as totp_service
from accounts.tasks import AccountEmail

pytestmark = pytest.mark.django_db

WRONG = "000000"


def _correct(user: RevelUser) -> str:
    return pyotp.TOTP(user.totp_secret).now()


def _fail_until_locked(user: RevelUser) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert totp_service.verify_totp(user, WRONG, reason="login") is False
    with pytest.raises(TOTPLockedError):
        totp_service.verify_totp(user, WRONG, reason="login")


@pytest.fixture
def mock_email() -> t.Generator[MagicMock]:
    with patch("accounts.tasks.send_account_email.delay") as delay:
        yield delay


def test_correct_code_returns_true(totp_user: RevelUser) -> None:
    assert totp_service.verify_totp(totp_user, _correct(totp_user), reason="login") is True


def test_wrong_codes_below_threshold_return_false(totp_user: RevelUser) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert totp_service.verify_totp(totp_user, WRONG, reason="login") is False


def test_threshold_failure_locks_and_sends_exactly_one_email(
    totp_user: RevelUser, mock_email: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        _fail_until_locked(totp_user)

    mock_email.assert_called_once()
    args, kwargs = mock_email.call_args
    assert args == (AccountEmail.TOTP_LOCKED, totp_user.email)
    assert kwargs["context"]["language"] == totp_user.language


def test_lock_is_logged_with_reason_and_no_pii(totp_user: RevelUser, mock_email: MagicMock) -> None:
    with patch("accounts.service.totp.logger") as log:
        _fail_until_locked(totp_user)

    log.warning.assert_called_once_with("totp_verification_locked", user_id=str(totp_user.id), reason="login")


def test_correct_code_is_refused_while_locked(totp_user: RevelUser, mock_email: MagicMock) -> None:
    _fail_until_locked(totp_user)

    with pytest.raises(TOTPLockedError):
        totp_service.verify_totp(totp_user, _correct(totp_user), reason="login")


def test_retry_after_is_within_the_lock_window(totp_user: RevelUser, mock_email: MagicMock) -> None:
    _fail_until_locked(totp_user)

    with pytest.raises(TOTPLockedError) as exc_info:
        totp_service.verify_totp(totp_user, WRONG, reason="login")

    assert 1 <= exc_info.value.retry_after <= totp_service.LOCK_SECONDS


def test_attempts_while_locked_neither_extend_nor_email_again(
    totp_user: RevelUser, mock_email: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        _fail_until_locked(totp_user)
    lock_key = totp_service._lock_key(totp_user)
    unlock_at = cache.get(lock_key)

    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(3):
            with pytest.raises(TOTPLockedError):
                totp_service.verify_totp(totp_user, WRONG, reason="login")

    assert cache.get(lock_key) == unlock_at
    mock_email.assert_called_once()


def test_success_resets_the_failure_counter(totp_user: RevelUser, mock_email: MagicMock) -> None:
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        totp_service.verify_totp(totp_user, WRONG, reason="login")
    assert totp_service.verify_totp(totp_user, _correct(totp_user), reason="login") is True

    # A fresh run of failures starts from zero: four more still do not lock.
    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert totp_service.verify_totp(totp_user, WRONG, reason="login") is False
    mock_email.assert_not_called()


def test_lock_expires(totp_user: RevelUser, mock_email: MagicMock) -> None:
    _fail_until_locked(totp_user)

    with patch("time.time", return_value=time.time() + totp_service.LOCK_SECONDS + 1):  # LocMem reads this
        assert cache.get(totp_service._lock_key(totp_user)) is None

    assert totp_service.verify_totp(totp_user, _correct(totp_user), reason="login") is True


def test_counter_restarts_after_a_lock(totp_user: RevelUser, mock_email: MagicMock) -> None:
    _fail_until_locked(totp_user)
    cache.delete(totp_service._lock_key(totp_user))

    for _ in range(totp_service.FAILURE_THRESHOLD - 1):
        assert totp_service.verify_totp(totp_user, WRONG, reason="login") is False


def test_lock_is_per_account(totp_user: RevelUser, user: RevelUser, mock_email: MagicMock) -> None:
    _fail_until_locked(totp_user)

    assert totp_service.verify_totp(user, WRONG, reason="login") is False


def test_counter_survives_a_missing_key_on_incr(totp_user: RevelUser) -> None:
    """The key expiring between ``add`` and ``incr`` must count the failure, not 500."""
    with patch("accounts.service.totp.cache.incr", side_effect=ValueError):
        assert totp_service.verify_totp(totp_user, WRONG, reason="login") is False

    assert cache.get(totp_service._fail_key(totp_user)) == 1


def test_attempt_is_reserved_before_verifying(totp_user: RevelUser, mock_email: MagicMock) -> None:
    """Concurrent requests that already passed the lock check must not get a sixth verification.

    Simulate the interleaving: the counter already holds the full budget when this request arrives
    (other in-flight requests reserved it). Even the correct code is refused, and unverified.
    """
    cache.set(totp_service._fail_key(totp_user), totp_service.FAILURE_THRESHOLD, timeout=60)

    code = _correct(totp_user)
    with patch("accounts.service.totp.pyotp.TOTP") as totp_cls:
        with pytest.raises(TOTPLockedError):
            totp_service.verify_totp(totp_user, code, reason="login")

    totp_cls.assert_not_called()
    assert cache.get(totp_service._lock_key(totp_user)) is not None  # the lock is ensured, not left to chance


def test_over_budget_attempt_locks_and_emails_once(
    totp_user: RevelUser, mock_email: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    cache.set(totp_service._fail_key(totp_user), totp_service.FAILURE_THRESHOLD + 3, timeout=60)

    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(2):
            with pytest.raises(TOTPLockedError):
                totp_service.verify_totp(totp_user, WRONG, reason="login")

    mock_email.assert_called_once()


def test_fifth_reserved_attempt_with_a_wrong_code_locks(totp_user: RevelUser, mock_email: MagicMock) -> None:
    cache.set(totp_service._fail_key(totp_user), totp_service.FAILURE_THRESHOLD - 1, timeout=60)

    with pytest.raises(TOTPLockedError):
        totp_service.verify_totp(totp_user, WRONG, reason="login")

    assert cache.get(totp_service._fail_key(totp_user)) is None
    assert cache.get(totp_service._lock_key(totp_user)) is not None


def test_fifth_reserved_attempt_with_the_correct_code_succeeds_and_resets(
    totp_user: RevelUser, mock_email: MagicMock
) -> None:
    cache.set(totp_service._fail_key(totp_user), totp_service.FAILURE_THRESHOLD - 1, timeout=60)

    assert totp_service.verify_totp(totp_user, _correct(totp_user), reason="login") is True

    assert cache.get(totp_service._fail_key(totp_user)) is None
    assert cache.get(totp_service._lock_key(totp_user)) is None


def test_lock_set_after_the_first_read_still_refuses_unverified(totp_user: RevelUser, mock_email: MagicMock) -> None:
    """A request that read "no lock" just before the 5th attempt locked (and reset the counter) is not verified."""
    real_incr = cache.incr

    def incr_after_a_concurrent_lock(key: str, *args: t.Any, **kwargs: t.Any) -> int:
        cache.add(totp_service._lock_key(totp_user), time.time() + totp_service.LOCK_SECONDS)
        return real_incr(key, *args, **kwargs)

    with (
        patch.object(cache, "incr", side_effect=incr_after_a_concurrent_lock),
        patch("accounts.service.totp.pyotp.TOTP") as totp_cls,
        pytest.raises(TOTPLockedError),
    ):
        totp_service.verify_totp(totp_user, "123456", reason="login")
    totp_cls.assert_not_called()
