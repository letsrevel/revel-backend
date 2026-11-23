"""The sweep queues a self-service deletion again when its run never came to an end (#1476)."""

import typing as t
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib import admin as django_admin
from django.utils import timezone
from django_celery_beat.models import PeriodicTask

from accounts.admin.user import RevelUserAdmin
from accounts.models import RevelUser
from accounts.service.global_ban_service import deactivate_user_for_ban
from accounts.tasks import delete_user_account, requeue_stale_account_deletions
from accounts.tasks.gdpr import STALE_ACCOUNT_DELETION_AFTER
from conftest import RevelUserFactory
from events.models import Event, Ticket
from events.tests.test_account_erasure.conftest import make_ticket

pytestmark = pytest.mark.django_db

Factory = t.Callable[[timedelta], Event]
_DELAY = "accounts.tasks.gdpr.delete_user_account.delay"


def _mark(user: RevelUser, age: timedelta, *, is_active: bool = False) -> datetime:
    """Give ``user`` the state that the self-service confirm leaves, with a mark of the given age."""
    mark = timezone.now() - age
    RevelUser.objects.filter(pk=user.pk).update(is_active=is_active, deletion_requested_at=mark)
    return mark


def _state(user: RevelUser) -> tuple[bool, datetime | None]:
    return RevelUser.objects.values_list("is_active", "deletion_requested_at").get(pk=user.pk)


@pytest.fixture
def stale(holder: RevelUser) -> tuple[RevelUser, datetime]:
    """An inactive account whose mark is one minute older than the threshold."""
    return holder, _mark(holder, STALE_ACCOUNT_DELETION_AFTER + timedelta(minutes=1))


def test_the_threshold_is_longer_than_the_longest_life_of_one_run(settings: t.Any) -> None:
    """Every retry waits the capped backoff at most, and every attempt can use the task time limit."""
    task: t.Any = delete_user_account
    longest = timedelta(
        seconds=task.max_retries * task.retry_backoff_max + (task.max_retries + 1) * settings.CELERY_TASK_TIME_LIMIT
    )

    assert STALE_ACCOUNT_DELETION_AFTER >= longest + timedelta(hours=6)


def test_a_stale_mark_gets_a_new_mark_and_a_run_that_carries_it(stale: tuple[RevelUser, datetime]) -> None:
    user, old_mark = stale
    before = timezone.now()

    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 1}

    is_active, new_mark = _state(user)
    assert not is_active
    assert new_mark is not None and new_mark >= before > old_mark
    task.assert_called_once_with(str(user.pk), deletion_mark=new_mark.isoformat())


def test_the_next_sweep_does_not_queue_the_account_again(stale: tuple[RevelUser, datetime]) -> None:
    with patch(_DELAY) as task:
        requeue_stale_account_deletions()
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_called_once()


def test_a_mark_younger_than_the_threshold_is_left_alone(holder: RevelUser) -> None:
    mark = _mark(holder, STALE_ACCOUNT_DELETION_AFTER - timedelta(minutes=1))

    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_not_called()
    assert _state(holder) == (False, mark)


def test_an_account_without_a_mark_is_left_alone(holder: RevelUser, revel_user_factory: RevelUserFactory) -> None:
    """An active account, and one that the unverified sweep or an admin deactivated: no mark, no run."""
    inactive = revel_user_factory.create_user(username="inactive@example.com", email="inactive@example.com")
    RevelUser.objects.filter(pk=inactive.pk).update(is_active=False)

    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_not_called()
    assert _state(holder) == (True, None)
    assert _state(inactive) == (False, None)


def test_an_active_account_is_left_alone_also_with_an_old_mark(holder: RevelUser) -> None:
    mark = _mark(holder, STALE_ACCOUNT_DELETION_AFTER + timedelta(days=1), is_active=True)

    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_not_called()
    assert _state(holder) == (True, mark)


def test_an_erased_account_is_left_alone_also_with_an_old_mark(stale: tuple[RevelUser, datetime]) -> None:
    user, mark = stale
    RevelUser.objects.filter(pk=user.pk).update(erased_at=timezone.now())

    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_not_called()
    assert _state(user) == (False, mark)


def test_a_banned_account_is_left_alone(stale: tuple[RevelUser, datetime]) -> None:
    """The ban cleared the mark: the account stays inactive, and no deletion is queued for it."""
    user, _mark_before = stale
    deactivate_user_for_ban(RevelUser.objects.get(pk=user.pk), "spam")

    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_not_called()
    assert _state(user) == (False, None)


def test_an_account_that_an_admin_deleted_is_left_alone(
    stale: tuple[RevelUser, datetime], admin_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The admin delete cleared the mark and queued its own run: the sweep adds none."""
    user, _mark_before = stale
    request = type("Request", (), {"user": admin_user})()
    with patch("accounts.tasks.gdpr.delete_user_account") as admin_task:
        with django_capture_on_commit_callbacks(execute=True):
            RevelUserAdmin(RevelUser, django_admin.site).delete_model(request, RevelUser.objects.get(pk=user.pk))
    admin_task.delay.assert_called_once_with(str(user.pk))

    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_not_called()
    assert _state(user) == (False, None)


@pytest.mark.parametrize("still_stale", [False, True], ids=["new-mark", "other-stale-mark"])
def test_a_sweep_that_loses_the_compare_and_set_queues_nothing(
    stale: tuple[RevelUser, datetime], still_stale: bool
) -> None:
    """The mark changes after this sweep read it: another sweep took the account, or the mark is another one.

    The second case has a mark that is still older than the threshold, so only the comparison with the value
    that was read stops the update.
    """
    user, old_mark = stale
    real_now = timezone.now
    taken_by_the_other_sweep = old_mark - timedelta(hours=1) if still_stale else real_now() + timedelta(seconds=5)
    calls = 0

    def now_with_an_overlapping_sweep() -> datetime:
        nonlocal calls
        calls += 1
        if calls == 2:  # the first call is the cutoff; the second one is the new mark, after the read
            assert RevelUser.objects.filter(pk=user.pk, deletion_requested_at=old_mark).update(
                deletion_requested_at=taken_by_the_other_sweep
            )
        return real_now()

    with patch(_DELAY) as task, patch("django.utils.timezone.now", side_effect=now_with_an_overlapping_sweep):
        assert requeue_stale_account_deletions() == {"requeued": 0}

    assert calls == 2
    task.assert_not_called()
    assert _state(user) == (False, taken_by_the_other_sweep)


def test_a_mark_that_is_cleared_after_the_read_queues_nothing(stale: tuple[RevelUser, datetime]) -> None:
    """A ban commits between the read of the sweep and its update."""
    user, _old_mark = stale
    real_now = timezone.now
    calls = 0

    def now_with_a_ban() -> datetime:
        nonlocal calls
        calls += 1
        if calls == 2:
            RevelUser.objects.filter(pk=user.pk).update(deletion_requested_at=None)
        return real_now()

    with patch(_DELAY) as task, patch("django.utils.timezone.now", side_effect=now_with_a_ban):
        assert requeue_stale_account_deletions() == {"requeued": 0}

    task.assert_not_called()
    assert _state(user) == (False, None)


def test_a_failed_dispatch_puts_the_old_mark_back_so_the_next_sweep_tries_again(
    stale: tuple[RevelUser, datetime],
) -> None:
    user, old_mark = stale

    with patch(_DELAY, side_effect=ConnectionError("broker down")), pytest.raises(ConnectionError):
        requeue_stale_account_deletions()

    assert _state(user) == (False, old_mark)
    with patch(_DELAY) as task:
        assert requeue_stale_account_deletions() == {"requeued": 1}
    task.assert_called_once()


def test_a_failed_dispatch_does_not_write_over_a_mark_that_replaced_the_new_one(
    stale: tuple[RevelUser, datetime],
) -> None:
    """The mark changes while the dispatch fails: the old mark goes back only onto the mark that the sweep wrote."""
    user, old_mark = stale
    replacement = timezone.now() + timedelta(seconds=30)

    def replace_then_fail(*args: t.Any, **kwargs: t.Any) -> None:
        assert (
            RevelUser.objects.filter(pk=user.pk)
            .exclude(deletion_requested_at=old_mark)
            .update(deletion_requested_at=replacement)
        )
        raise ConnectionError("broker down")

    with patch(_DELAY, side_effect=replace_then_fail), pytest.raises(ConnectionError):
        requeue_stale_account_deletions()

    assert _state(user) == (False, replacement)


def test_one_sweep_takes_the_oldest_marks_up_to_the_batch_size(
    stale: tuple[RevelUser, datetime], revel_user_factory: RevelUserFactory
) -> None:
    newer, newer_mark = stale
    older = revel_user_factory.create_user(username="older@example.com", email="older@example.com")
    _mark(older, STALE_ACCOUNT_DELETION_AFTER + timedelta(days=1))

    with patch(_DELAY) as task, patch("accounts.tasks.gdpr.STALE_ACCOUNT_DELETION_BATCH", 1):
        assert requeue_stale_account_deletions() == {"requeued": 1}

    assert task.call_args.args == (str(older.pk),)
    assert _state(newer) == (False, newer_mark)


def test_the_requeued_run_deletes_an_account_without_holdings(stale: tuple[RevelUser, datetime]) -> None:
    """Tasks run eagerly in tests: the sweep queues the real deletion task."""
    user, _old_mark = stale

    assert requeue_stale_account_deletions() == {"requeued": 1}

    assert not RevelUser.objects.filter(pk=user.pk).exists()


def test_the_requeued_run_erases_an_account_with_a_kept_ticket(
    stale: tuple[RevelUser, datetime], ended_event_factory: Factory
) -> None:
    user, _old_mark = stale
    ticket = make_ticket(user, ended_event_factory(-timedelta(days=2)))

    assert requeue_stale_account_deletions() == {"requeued": 1}

    user.refresh_from_db()
    assert user.erased_at is not None
    assert _state(user) == (False, None)
    assert Ticket.objects.filter(pk=ticket.pk).exists()


def test_a_requeued_run_that_fails_for_good_gives_the_access_back(
    stale: tuple[RevelUser, datetime], ended_event_factory: Factory
) -> None:
    """The new run carries the new mark, so its final failure is the one that restores the access."""
    user, _old_mark = stale
    make_ticket(user, ended_event_factory(-timedelta(days=2)))

    with (
        patch("accounts.service.account_erasure.erase_account_keeping_holdings", side_effect=RuntimeError("x")),
        pytest.raises(RuntimeError),
    ):
        requeue_stale_account_deletions()

    assert _state(user) == (True, None)


def test_the_beat_row_runs_the_registered_task_every_15_minutes() -> None:
    row = PeriodicTask.objects.get(name="Requeue stale account deletions")

    assert row.task == requeue_stale_account_deletions.name == "accounts.requeue_stale_account_deletions"
    assert row.enabled
    assert (row.interval.every, row.interval.period) == (15, "minutes")
