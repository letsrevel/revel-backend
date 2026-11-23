"""A deletion run with a mark works only while the account still waits for that run (#1476)."""

import typing as t
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from django.contrib import admin as django_admin
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.admin.user import RevelUserAdmin
from accounts.models import GlobalBan, RevelUser
from accounts.service import global_ban_service
from accounts.service.account_erasure import restore_access_after_failed_deletion
from accounts.tasks import delete_user_account, requeue_stale_account_deletions
from accounts.tasks.gdpr import STALE_ACCOUNT_DELETION_AFTER
from accounts.utils.email_normalization import normalize_email_for_matching
from conftest import RevelUserFactory
from events.models import Event, Ticket
from events.tests.test_account_erasure.conftest import HOLDER_EMAIL, make_ticket

pytestmark = pytest.mark.django_db

Factory = t.Callable[[timedelta], Event]
_DELAY = "accounts.tasks.gdpr.delete_user_account.delay"
_CANCEL = "accounts.tasks.gdpr._cancel_live_subscriptions"
_REFERRALS = "accounts.tasks.gdpr.cleanup_referral_data"
_RELEASE = "events.service.account_deletion_service.release_capacity_of_account"
_ERASE = "accounts.service.account_erasure.erase_account_keeping_holdings"


def _pend(user: RevelUser, age: timedelta = timedelta(minutes=1)) -> datetime:
    """Give ``user`` the state that the self-service confirm leaves; return its mark."""
    mark = timezone.now() - age
    RevelUser.objects.filter(pk=user.pk).update(is_active=False, deletion_requested_at=mark)
    return mark


def _state(user: RevelUser) -> tuple[bool, datetime | None, datetime | None]:
    return RevelUser.objects.values_list("is_active", "deletion_requested_at", "erased_at").get(pk=user.pk)


def _run(user: RevelUser, mark: datetime | None) -> None:
    kwargs = {"deletion_mark": mark.isoformat()} if mark else {}
    delete_user_account.apply(args=(str(user.pk),), kwargs=kwargs)


def _run_expecting_no_work(user: RevelUser, mark: datetime) -> None:
    """Run the task for ``mark``; it must not touch subscriptions, referrals, capacity or the account."""
    with (
        patch(_CANCEL) as cancel,
        patch(_REFERRALS) as referrals,
        patch(_RELEASE) as release,
        patch("accounts.tasks.gdpr.logger") as log,
    ):
        _run(user, mark)  # ends normally: no error, so no retry

    cancel.assert_not_called()
    referrals.assert_not_called()
    release.assert_not_called()
    log.info.assert_called_once_with("account_deletion_skipped_mark_changed", user_id=str(user.pk))
    log.error.assert_not_called()  # no final-failure handling, so no restore


# A change of the account state that commits while a run is at work (built at call time: a fresh timestamp).
_CHANGES: dict[str, t.Callable[[], dict[str, t.Any]]] = {
    "reactivated": lambda: {"is_active": True, "deletion_requested_at": None},
    "new-mark": lambda: {"deletion_requested_at": timezone.now()},
    "mark-cleared": lambda: {"deletion_requested_at": None},
}


@pytest.fixture
def with_ticket(holder: RevelUser, ended_event_factory: Factory) -> Ticket:
    """A kept ticket: the account is erased in place, not deleted."""
    return make_ticket(holder, ended_event_factory(-timedelta(days=2)))


class TestARunWhoseMarkIsGone:
    def test_an_old_run_does_nothing_after_the_sweep_run_failed_and_gave_the_access_back(
        self, holder: RevelUser, with_ticket: Ticket
    ) -> None:
        """Run 1 is queued for a long time; the sweep queues run 2; run 2 fails for good; then run 1 starts."""
        first_mark = _pend(holder, STALE_ACCOUNT_DELETION_AFTER + timedelta(minutes=1))
        with patch(_ERASE, side_effect=RuntimeError("x")), pytest.raises(RuntimeError):
            requeue_stale_account_deletions()  # eager: run 2 runs here and fails for good
        assert _state(holder) == (True, None, None)

        _run_expecting_no_work(holder, first_mark)

        assert _state(holder) == (True, None, None)
        holder.refresh_from_db()
        assert (holder.email, holder.first_name) == (HOLDER_EMAIL, "Ada")
        assert Ticket.objects.filter(pk=with_ticket.pk, user=holder).exists()

    def test_an_old_run_does_nothing_while_the_run_of_a_newer_mark_is_queued(self, holder: RevelUser) -> None:
        first_mark = _pend(holder, STALE_ACCOUNT_DELETION_AFTER + timedelta(minutes=1))
        with patch(_DELAY):
            requeue_stale_account_deletions()
        _is_active, second_mark, _erased = _state(holder)
        assert second_mark is not None and second_mark != first_mark

        _run_expecting_no_work(holder, first_mark)

        assert _state(holder) == (False, second_mark, None)  # still pending for the newer run
        _run(holder, second_mark)
        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    @pytest.mark.parametrize("keep_mark", [False, True], ids=["mark-cleared", "mark-kept"])
    def test_a_run_does_nothing_for_an_account_that_is_active_again(self, holder: RevelUser, keep_mark: bool) -> None:
        """An admin reactivated the account before the run started (the admin form also clears the mark).

        ``mark-kept`` is a row that cannot exist today: the check on ``is_active`` alone must stop the run.
        """
        mark = _pend(holder)
        RevelUser.objects.filter(pk=holder.pk).update(
            is_active=True, **({} if keep_mark else {"deletion_requested_at": None})
        )

        _run_expecting_no_work(holder, mark)

        assert _state(holder) == (True, mark if keep_mark else None, None)

    def test_a_run_does_nothing_after_an_admin_deactivated_the_account_and_cleared_the_mark(
        self, holder: RevelUser
    ) -> None:
        """An admin form that showed the account active was posted with ``is_active`` unticked: a cancel."""
        mark = _pend(holder)
        RevelUser.objects.filter(pk=holder.pk).update(deletion_requested_at=None)

        _run_expecting_no_work(holder, mark)

        assert _state(holder) == (False, None, None)

    def test_a_run_whose_final_failure_restore_already_ran_does_nothing(self, holder: RevelUser) -> None:
        mark = _pend(holder)
        assert restore_access_after_failed_deletion(str(holder.pk), mark)

        _run_expecting_no_work(holder, mark)

        assert _state(holder) == (True, None, None)


class TestAChangeBetweenTheFirstCheckAndTheLock:
    @pytest.mark.parametrize("change", ["reactivated", "new-mark", "mark-cleared"])
    def test_the_run_stops_under_the_lock(self, holder: RevelUser, with_ticket: Ticket, change: str) -> None:
        """The change commits while the referral cleanup runs, after the first check of the run."""
        mark = _pend(holder)
        after = _CHANGES[change]()

        def change_meanwhile(user: RevelUser) -> None:
            RevelUser.objects.filter(pk=user.pk).update(**after)

        with (
            patch(_REFERRALS, side_effect=change_meanwhile) as referrals,
            patch(_RELEASE) as release,
            patch(_ERASE) as erase,
            patch("accounts.service.gdpr.purge_user_history") as purge,
            patch("accounts.tasks.gdpr.logger") as log,
        ):
            _run(holder, mark)

        referrals.assert_called_once()  # the first check passed
        release.assert_not_called()
        erase.assert_not_called()
        purge.assert_not_called()
        log.info.assert_any_call("account_deletion_skipped_mark_changed", user_id=str(holder.pk))
        log.error.assert_not_called()
        holder.refresh_from_db()
        assert holder.erased_at is None
        assert holder.email == HOLDER_EMAIL
        assert holder.is_active == after.get("is_active", False)
        assert holder.deletion_requested_at == after["deletion_requested_at"]
        assert Ticket.objects.filter(pk=with_ticket.pk).exists()

    @pytest.mark.parametrize("change", ["reactivated", "new-mark", "mark-cleared"])
    def test_a_change_during_the_subscription_cancel_stops_the_run_before_the_referral_cleanup(
        self, holder: RevelUser, with_ticket: Ticket, change: str
    ) -> None:
        """The cancel has its own check and runs; the cleanup reads the account again and does not start."""
        mark = _pend(holder)
        after = _CHANGES[change]()

        def change_meanwhile(user: RevelUser) -> None:
            RevelUser.objects.filter(pk=user.pk).update(**after)

        with (
            patch(_CANCEL, side_effect=change_meanwhile) as cancel,
            patch(_REFERRALS) as referrals,
            patch(_RELEASE) as release,
            patch(_ERASE) as erase,
            patch("accounts.tasks.gdpr.logger") as log,
        ):
            _run(holder, mark)

        cancel.assert_called_once()
        referrals.assert_not_called()
        release.assert_not_called()
        erase.assert_not_called()
        log.info.assert_any_call("account_deletion_skipped_mark_changed", user_id=str(holder.pk))
        log.error.assert_not_called()
        holder.refresh_from_db()
        assert holder.erased_at is None
        assert holder.deletion_requested_at == after["deletion_requested_at"]

    def test_a_change_after_the_first_read_stops_the_run_before_the_subscription_cancel(
        self, holder: RevelUser
    ) -> None:
        """The change commits after the task read the account and before the cancel: a fresh read sees it."""
        mark = _pend(holder)

        def reactivate(user: RevelUser, force_full_delete: bool) -> bool:
            RevelUser.objects.filter(pk=user.pk).update(is_active=True, deletion_requested_at=None)
            return False

        with (
            patch("accounts.tasks.gdpr._keeps_erased_row", side_effect=reactivate) as first_read_done,
            patch(_CANCEL) as cancel,
            patch(_REFERRALS) as referrals,
        ):
            _run(holder, mark)

        first_read_done.assert_called_once()
        cancel.assert_not_called()
        referrals.assert_not_called()
        assert _state(holder) == (True, None, None)

    def test_an_account_that_is_deleted_during_the_cancel_ends_the_run(self, holder: RevelUser) -> None:
        mark = _pend(holder)

        def delete_meanwhile(user: RevelUser) -> None:
            RevelUser.objects.filter(pk=user.pk).delete()

        with patch(_CANCEL, side_effect=delete_meanwhile), patch(_REFERRALS) as referrals:
            _run(holder, mark)

        referrals.assert_not_called()

    def test_a_run_without_a_mark_reads_nothing_more_and_is_not_stopped(self, holder: RevelUser) -> None:
        """An admin run: a change during the cancel does not stop it."""
        _pend(holder)

        def reactivate(user: RevelUser) -> None:
            RevelUser.objects.filter(pk=user.pk).update(is_active=True, deletion_requested_at=None)

        with patch(_CANCEL, side_effect=reactivate):
            _run(holder, None)

        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    def test_a_full_delete_stops_under_the_lock_too(self, holder: RevelUser) -> None:
        mark = _pend(holder)

        def reactivate(user: RevelUser) -> None:
            RevelUser.objects.filter(pk=user.pk).update(is_active=True, deletion_requested_at=None)

        with patch(_REFERRALS, side_effect=reactivate):
            _run(holder, mark)

        assert _state(holder) == (True, None, None)


class TestARunWhoseMarkStillMatches:
    def test_it_deletes_an_account_without_holdings(self, holder: RevelUser) -> None:
        _run(holder, _pend(holder))

        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    def test_it_erases_an_account_with_a_kept_ticket(self, holder: RevelUser, with_ticket: Ticket) -> None:
        _run(holder, _pend(holder))

        is_active, mark, erased_at = _state(holder)
        assert (is_active, mark) == (False, None)
        assert erased_at is not None
        assert Ticket.objects.filter(pk=with_ticket.pk).exists()

    def test_its_final_failure_still_gives_the_access_back(self, holder: RevelUser, with_ticket: Ticket) -> None:
        mark = _pend(holder)

        with patch(_ERASE, side_effect=RuntimeError("x")), pytest.raises(RuntimeError):
            _run(holder, mark)

        assert _state(holder) == (True, None, None)


class TestARunWithoutAMark:
    @pytest.mark.parametrize(
        "state",
        [
            {"is_active": True},
            {"is_active": False},
            {"is_active": False, "deletion_requested_at": timezone.now()},
            {"is_active": True, "deletion_requested_at": timezone.now()},
        ],
        ids=["active", "inactive", "pending", "active-with-mark"],
    )
    def test_it_deletes_the_account_in_every_state(self, holder: RevelUser, state: dict[str, t.Any]) -> None:
        """Admin delete, ban, the unverified sweep and messages of older code: no check on the mark."""
        RevelUser.objects.filter(pk=holder.pk).update(**state)

        _run(holder, None)

        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    def test_it_erases_an_account_with_a_kept_ticket(self, holder: RevelUser, with_ticket: Ticket) -> None:
        _run(holder, None)

        assert _state(holder)[2] is not None


class TestABanOfAPendingAccount:
    """The ban clears the mark, so the run of the confirm stops: the ban queues the deletion itself."""

    def _ban(self, ban_type: str, value: str) -> GlobalBan:
        # bulk_create: no post_save signal, so the test calls the ban processing itself.
        normalized = value if ban_type == GlobalBan.BanType.DOMAIN else normalize_email_for_matching(value)
        return GlobalBan.objects.bulk_create([GlobalBan(ban_type=ban_type, value=value, normalized_value=normalized)])[
            0
        ]

    def test_an_email_ban_queues_one_run_without_a_mark_on_commit(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The inactive branch: the ban reads the pending account as inactive."""
        mark = _pend(holder)
        ban = self._ban(GlobalBan.BanType.EMAIL, holder.email)

        with patch(_DELAY) as task:
            with django_capture_on_commit_callbacks(execute=True):
                global_ban_service.process_email_ban(ban)
                task.assert_not_called()  # queued on commit, not in the transaction of the ban
            task.assert_called_once_with(str(holder.pk))

        assert _state(holder) == (False, None, None)
        _run_expecting_no_work(holder, mark)  # the run of the confirm

    def test_the_run_of_the_ban_deletes_the_account(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _pend(holder)
        ban = self._ban(GlobalBan.BanType.EMAIL, holder.email)

        with django_capture_on_commit_callbacks(execute=True):
            global_ban_service.process_email_ban(ban)  # eager: the queued run runs at commit

        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    def test_the_run_of_the_ban_erases_an_account_with_a_kept_ticket(
        self, holder: RevelUser, with_ticket: Ticket, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _pend(holder)
        ban = self._ban(GlobalBan.BanType.EMAIL, holder.email)

        with django_capture_on_commit_callbacks(execute=True):
            global_ban_service.process_email_ban(ban)

        is_active, mark, erased_at = _state(holder)
        assert (is_active, mark) == (False, None)
        assert erased_at is not None
        assert Ticket.objects.filter(pk=with_ticket.pk).exists()

    def test_a_failed_run_of_the_ban_leaves_the_account_inactive(
        self, holder: RevelUser, with_ticket: Ticket, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _pend(holder)

        with patch(_DELAY) as task, django_capture_on_commit_callbacks(execute=True):
            global_ban_service.deactivate_user_for_ban(RevelUser.objects.get(pk=holder.pk), "spam")
        with patch(_ERASE, side_effect=RuntimeError("x")), pytest.raises(RuntimeError):
            delete_user_account.apply(args=task.call_args.args, kwargs=dict(task.call_args.kwargs))

        assert _state(holder) == (False, None, None)

    def test_a_ban_that_read_the_account_before_the_confirm_queues_the_run_too(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The active branch: the ban holds an account object that is still active and has no mark."""
        read_by_the_ban = RevelUser.objects.get(pk=holder.pk)
        mark = _pend(holder)  # the confirm commits after that read

        with (
            patch(_DELAY) as task,
            patch("notifications.signals.notification_requested.send"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            global_ban_service.deactivate_user_for_ban(read_by_the_ban, "spam")

        task.assert_called_once_with(str(holder.pk))
        assert _state(holder) == (False, None, None)
        _run_expecting_no_work(holder, mark)

    def test_a_domain_ban_leaves_a_pending_account_to_the_run_of_its_confirm(
        self, holder: RevelUser, revel_user_factory: RevelUserFactory, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """A domain ban deactivates active accounts only: the pending one keeps its mark and its own run."""
        mark = _pend(holder)
        active = revel_user_factory.create_user(username="other@example.com", email="other@example.com")
        ban = self._ban(GlobalBan.BanType.DOMAIN, "example.com")

        with (
            patch(_DELAY) as task,
            patch("notifications.signals.notification_requested.send"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            assert global_ban_service.process_domain_ban(ban) == 1

        task.assert_not_called()
        assert _state(active) == (False, None, None)
        assert _state(holder) == (False, mark, None)
        _run(holder, mark)
        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    def test_a_domain_ban_of_an_account_that_confirmed_after_the_read_queues_one_run(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The domain loop read the account as active; the confirm commits before the loop reaches it."""
        ban = self._ban(GlobalBan.BanType.DOMAIN, "example.com")
        real = global_ban_service.deactivate_user_for_ban

        def confirm_meanwhile(user: RevelUser, reason: str) -> None:
            _pend(holder)
            real(user, reason)

        with (
            patch(_DELAY) as task,
            patch("notifications.signals.notification_requested.send"),
            patch.object(global_ban_service, "deactivate_user_for_ban", side_effect=confirm_meanwhile),
            django_capture_on_commit_callbacks(execute=True),
        ):
            assert global_ban_service.process_domain_ban(ban) == 1

        task.assert_called_once_with(str(holder.pk))
        assert _state(holder) == (False, None, None)

    @pytest.mark.parametrize("is_active", [True, False])
    def test_a_ban_of_an_account_without_a_mark_queues_nothing(
        self, holder: RevelUser, is_active: bool, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        RevelUser.objects.filter(pk=holder.pk).update(is_active=is_active)

        with (
            patch(_DELAY) as task,
            patch("notifications.signals.notification_requested.send"),
            django_capture_on_commit_callbacks(execute=True),
        ):
            global_ban_service.deactivate_user_for_ban(RevelUser.objects.get(pk=holder.pk), "spam")
            global_ban_service.deactivate_user_for_ban(RevelUser.objects.get(pk=holder.pk), "spam")

        task.assert_not_called()
        assert _state(holder) == (False, None, None)

    def test_a_second_ban_of_the_same_account_queues_no_second_run(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _pend(holder)

        with patch(_DELAY) as task, django_capture_on_commit_callbacks(execute=True):
            global_ban_service.deactivate_user_for_ban(RevelUser.objects.get(pk=holder.pk), "spam")
            global_ban_service.deactivate_user_for_ban(RevelUser.objects.get(pk=holder.pk), "spam")

        task.assert_called_once_with(str(holder.pk))


def test_an_admin_delete_of_a_pending_account_stops_the_run_of_the_confirm(
    holder: RevelUser, admin_user: RevelUser, django_capture_on_commit_callbacks: t.Any
) -> None:
    """The admin delete cleared the mark and queued its own run: the first run must not work next to it."""
    mark = _pend(holder)
    request = type("Request", (), {"user": admin_user})()
    with patch(_DELAY) as task, django_capture_on_commit_callbacks(execute=True):
        RevelUserAdmin(RevelUser, django_admin.site).delete_model(request, RevelUser.objects.get(pk=holder.pk))
    task.assert_called_once_with(str(holder.pk))

    _run_expecting_no_work(holder, mark)

    _run(holder, None)  # the admin run
    assert not RevelUser.objects.filter(pk=holder.pk).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("read_as_active", [False, True], ids=["inactive-branch", "active-branch"])
def test_a_ban_outside_a_transaction_reads_the_mark_under_the_row_lock(holder: RevelUser, read_as_active: bool) -> None:
    """transaction=True: the domain-ban task calls the ban in autocommit, with no outer transaction.

    The read of the mark and its clear must be one step for a deletion confirm, so the read locks the row
    inside the transaction of the clear. In the wrapping transaction of a normal test a missing ``atomic``
    block cannot fail, and the dispatch on commit never runs.
    """
    read_by_the_ban = RevelUser.objects.get(pk=holder.pk) if read_as_active else None
    _pend(holder)
    user = read_by_the_ban or RevelUser.objects.get(pk=holder.pk)

    with (
        patch(_DELAY) as task,
        patch("notifications.signals.notification_requested.send"),
        CaptureQueriesContext(connection) as queries,
    ):
        global_ban_service.deactivate_user_for_ban(user, "spam")

    statements = [query["sql"] for query in queries.captured_queries]
    read = next(i for i, sql in enumerate(statements) if "FOR UPDATE" in sql and "deletion_requested_at" in sql)
    clear = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE") and "deletion_requested_at" in sql)
    begin = max(i for i, sql in enumerate(statements[:read]) if sql == "BEGIN")
    commit = next(i for i, sql in enumerate(statements) if i > read and sql == "COMMIT")
    assert begin < read < clear < commit  # one transaction: the lock of the read is held until the clear commits
    task.assert_called_once_with(str(holder.pk))  # on commit, which here is the end of that transaction
    assert _state(holder) == (False, None, None)
