"""A restore after a failed deletion and a ban of the same email never both end with the account active (#1476)."""

import typing as t
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.db.models import Q
from django.utils import timezone

from accounts.models import GlobalBan, RevelUser
from accounts.service import global_ban_service
from accounts.service.account_erasure import restore_access_after_failed_deletion
from accounts.tasks import delete_user_account, requeue_stale_account_deletions
from accounts.tasks.gdpr import STALE_ACCOUNT_DELETION_AFTER

pytestmark = pytest.mark.django_db

# The restore UPDATE with a snapshot that is older than the ban: its ban subquery finds no row.
_UPDATE_DOES_NOT_SEE_THE_BAN = patch("accounts.service.account_erasure.email_ban_filter", return_value=Q(pk__in=[]))


def _pend(user: RevelUser) -> datetime:
    """Give ``user`` the state that the self-service confirm leaves; return its mark."""
    mark = timezone.now() - timedelta(minutes=1)
    RevelUser.objects.filter(pk=user.pk).update(is_active=False, deletion_requested_at=mark)
    return mark


def _ban_domain_of(user: RevelUser) -> GlobalBan:
    # bulk_create: no post_save signal, so the ban row exists and its task has not run.
    domain = user.email.split("@")[1]
    return GlobalBan.objects.bulk_create(
        [GlobalBan(ban_type=GlobalBan.BanType.DOMAIN, value=domain, normalized_value=domain)]
    )[0]


def _state(user: RevelUser) -> tuple[bool, datetime | None]:
    return RevelUser.objects.values_list("is_active", "deletion_requested_at").get(pk=user.pk)


def test_a_ban_that_committed_while_the_restore_ran_deactivates_the_account_after_the_commit(
    user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The domain ban task already skipped the account (it was inactive), and the UPDATE did not see the ban."""
    mark = _pend(user)
    _ban_domain_of(user)

    with (
        patch("accounts.service.global_ban_service.blacklist_user_tokens") as blacklist,
        patch("accounts.service.global_ban_service.logger") as log,
    ):
        with _UPDATE_DOES_NOT_SEE_THE_BAN, django_capture_on_commit_callbacks(execute=True):
            assert restore_access_after_failed_deletion(str(user.pk), mark)
            assert _state(user) == (True, None)  # the UPDATE restored the account; the check runs after the commit

    assert _state(user) == (False, mark)  # as after a refused restore: the deletion stays pending
    blacklist.assert_called_once()
    log.warning.assert_called_once_with("account_restore_undone_for_ban", user_id=str(user.pk))


def test_the_deletion_of_an_account_whose_restore_was_undone_is_queued_again_by_the_sweep(
    user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The user asked for the deletion: the ban must not end that request."""
    mark = _pend(user)
    _ban_domain_of(user)
    with _UPDATE_DOES_NOT_SEE_THE_BAN, django_capture_on_commit_callbacks(execute=True):
        assert restore_access_after_failed_deletion(str(user.pk), mark)
    RevelUser.objects.filter(pk=user.pk).update(
        deletion_requested_at=timezone.now() - STALE_ACCOUNT_DELETION_AFTER - timedelta(minutes=1)
    )

    assert requeue_stale_account_deletions() == {"requeued": 1}  # eager: the new run deletes the account

    assert not RevelUser.objects.filter(pk=user.pk).exists()


def test_an_error_of_the_check_does_not_replace_the_error_of_the_deletion(
    user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The check runs in the failure handling of the task: the task must still fail with its own error."""
    mark = _pend(user)

    with (
        patch(
            "accounts.service.account_erasure.deactivate_restored_account_if_banned", side_effect=OSError("db")
        ) as check,
        patch("accounts.tasks.gdpr.cleanup_referral_data", side_effect=RuntimeError("deletion failed")),
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError, match="deletion failed"),
    ):
        delete_user_account.apply(args=(str(user.pk),), kwargs={"deletion_mark": mark.isoformat()})

    check.assert_called_once_with(str(user.pk), mark)
    # The known limit: a check that fails before its UPDATE leaves the account as restored (see the docstring).
    assert _state(user) == (True, None)


@pytest.mark.parametrize("change", ["new-confirmation", "admin-deactivated", "active-with-a-mark"])
def test_the_check_does_not_write_over_a_state_that_changed_after_the_restore(user: RevelUser, change: str) -> None:
    """Each condition of the UPDATE alone: an inactive row, and an active row that has a mark again."""
    mark = _pend(user)
    _ban_domain_of(user)
    new_mark = timezone.now()
    changes: dict[str, dict[str, t.Any]] = {
        "new-confirmation": {"is_active": False, "deletion_requested_at": new_mark},
        "admin-deactivated": {"is_active": False},
        "active-with-a-mark": {"deletion_requested_at": new_mark},
    }
    after = changes[change]
    RevelUser.objects.filter(pk=user.pk).update(is_active=True, deletion_requested_at=None)  # as restored
    real = global_ban_service.is_email_globally_banned

    def change_meanwhile(email: str) -> bool:
        RevelUser.objects.filter(pk=user.pk).update(**after)
        return real(email)

    with patch.object(global_ban_service, "is_email_globally_banned", side_effect=change_meanwhile):
        assert not global_ban_service.deactivate_restored_account_if_banned(str(user.pk), mark)

    assert _state(user) == (after.get("is_active", True), after.get("deletion_requested_at"))


def test_an_error_after_the_undo_leaves_the_account_inactive_with_its_mark(
    user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The token step fails: the account is already inactive and pending, and the caller gets no error."""
    mark = _pend(user)
    _ban_domain_of(user)

    with (
        patch("accounts.service.global_ban_service.blacklist_user_tokens", side_effect=OSError("db")) as blacklist,
        patch("accounts.service.global_ban_service.logger") as log,
    ):
        with _UPDATE_DOES_NOT_SEE_THE_BAN, django_capture_on_commit_callbacks(execute=True):
            assert restore_access_after_failed_deletion(str(user.pk), mark)

    blacklist.assert_called_once()
    assert _state(user) == (False, mark)
    log.warning.assert_called_once_with("account_restore_undone_for_ban", user_id=str(user.pk))


def test_the_failed_run_of_the_task_ends_with_the_banned_account_inactive(
    user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    mark = _pend(user)
    _ban_domain_of(user)

    with (
        _UPDATE_DOES_NOT_SEE_THE_BAN,
        patch("accounts.tasks.gdpr.cleanup_referral_data", side_effect=RuntimeError("x")),
        patch("notifications.signals.notification_requested.send") as notify,
        django_capture_on_commit_callbacks(execute=True),
        pytest.raises(RuntimeError),
    ):
        delete_user_account.apply(args=(str(user.pk),), kwargs={"deletion_mark": mark.isoformat()})

    assert _state(user) == (False, mark)
    notify.assert_not_called()  # no ban notification to an account that asked for its deletion


def test_a_restore_without_a_ban_stays_active_after_the_commit(
    user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    mark = _pend(user)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        assert restore_access_after_failed_deletion(str(user.pk), mark)

    assert len(callbacks) == 1
    assert _state(user) == (True, None)


def test_a_refused_restore_registers_no_check(user: RevelUser, django_capture_on_commit_callbacks: t.Any) -> None:
    """The UPDATE sees the ban: nothing is restored, so there is nothing to check after the commit."""
    mark = _pend(user)
    _ban_domain_of(user)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        assert not restore_access_after_failed_deletion(str(user.pk), mark)

    assert callbacks == []
    assert _state(user) == (False, mark)


def test_a_domain_ban_that_commits_after_the_check_finds_the_restored_account_active(
    user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The other order: the restore and its check are done, then the ban commits and its task runs."""
    mark = _pend(user)
    with django_capture_on_commit_callbacks(execute=True):
        assert restore_access_after_failed_deletion(str(user.pk), mark)
    assert _state(user) == (True, None)

    ban = _ban_domain_of(user)
    with patch("notifications.signals.notification_requested.send"):
        assert global_ban_service.process_domain_ban(ban) == 1

    assert _state(user) == (False, None)


@pytest.mark.parametrize("state", ["inactive", "missing"])
def test_the_check_does_nothing_for_an_account_that_is_not_active_or_is_gone(user: RevelUser, state: str) -> None:
    _ban_domain_of(user)
    if state == "inactive":
        RevelUser.objects.filter(pk=user.pk).update(is_active=False)
    else:
        RevelUser.objects.filter(pk=user.pk).delete()

    with patch("accounts.service.global_ban_service.blacklist_user_tokens") as blacklist:
        assert not global_ban_service.deactivate_restored_account_if_banned(str(user.pk), timezone.now())

    blacklist.assert_not_called()
