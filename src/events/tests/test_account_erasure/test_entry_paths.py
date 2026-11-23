"""Every deletion path erases in place, and an erased account can no longer sign in (#1476)."""

import typing as t
import uuid
from datetime import timedelta
from unittest.mock import call, patch

import orjson
import pytest
from django.conf import settings
from django.http import Http404
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts import schema
from accounts.jwt import create_token
from accounts.models import EmailVerificationReminderTracking, ExternalIdentity, RevelUser, UserDataExport
from accounts.service import account as account_service
from accounts.service import oidc
from accounts.service.oidc import OIDCClaims
from accounts.tasks import (
    delete_old_inactive_accounts,
    delete_user_account,
    generate_user_data_export,
    requeue_stale_account_deletions,
)
from accounts.tasks.gdpr import STALE_ACCOUNT_DELETION_AFTER
from events.models import Event, Ticket
from events.tests.test_account_erasure.conftest import HOLDER_EMAIL, make_ticket
from revel.oidc_config import OIDCProviderConfig

pytestmark = pytest.mark.django_db

Factory = t.Callable[[timedelta], Event]
GOOGLE = OIDCProviderConfig(
    key="google", name="Google", issuer="https://accounts.google.com", client_id="c", client_secret="s"
)


def _deletion_token(user: RevelUser) -> str:
    payload = schema.DeleteAccountJWTPayloadSchema(
        user_id=user.id, email=user.email, exp=timezone.now() + settings.VERIFY_TOKEN_LIFETIME
    )
    return create_token(payload.model_dump(mode="json"), settings.SECRET_KEY, settings.JWT_ALGORITHM)


def test_confirming_the_deletion_deactivates_the_account_in_the_request(holder: RevelUser) -> None:
    """Sign-in stops at once, even before the background deletion runs."""
    with patch("accounts.tasks.delete_user_account.delay") as task:
        account_service.confirm_account_deletion(_deletion_token(holder))

    holder.refresh_from_db()
    assert not holder.is_active
    task.assert_not_called()  # on_commit: the test transaction never commits


@pytest.mark.django_db(transaction=True)
def test_self_service_deletion_erases_an_account_with_a_kept_ticket(
    holder: RevelUser, ended_event_factory: Factory
) -> None:
    """transaction=True: the deletion task is dispatched with transaction.on_commit."""
    ticket = make_ticket(holder, ended_event_factory(-timedelta(days=2)))

    account_service.confirm_account_deletion(_deletion_token(holder))

    holder.refresh_from_db()
    assert holder.erased_at is not None
    assert Ticket.objects.filter(pk=ticket.pk).exists()


def test_the_unverified_sweep_erases_and_then_skips_the_account(
    holder: RevelUser, ended_event_factory: Factory
) -> None:
    ticket = make_ticket(holder, ended_event_factory(-timedelta(days=2)))
    RevelUser.objects.filter(pk=holder.pk).update(is_active=False, email_verified=False)
    EmailVerificationReminderTracking.objects.create(
        user=holder, deactivation_email_sent_at=timezone.now() - timedelta(days=61)
    )

    delete_old_inactive_accounts()

    holder.refresh_from_db()
    assert holder.erased_at is not None
    assert Ticket.objects.filter(pk=ticket.pk).exists()
    # The tracking row went with the erasure, so the next sweep does not see the account again.
    assert not EmailVerificationReminderTracking.objects.filter(user_id=holder.pk).exists()


def _refresh(refresh: str) -> t.Any:
    return Client().post(
        reverse("api:token_refresh"), data=orjson.dumps({"refresh": refresh}), content_type="application/json"
    )


def _login(username: str, password: str) -> t.Any:
    return Client().post(
        reverse("api:token_obtain_pair"),
        data=orjson.dumps({"username": username, "password": password}),
        content_type="application/json",
    )


class TestSignInAfterErasure:
    @pytest.fixture
    def erased(self, holder: RevelUser, ended_event_factory: Factory) -> tuple[RevelUser, str, str]:
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        holder.set_password("old-password")
        holder.save()
        refresh = RefreshToken.for_user(holder)
        access = str(refresh.access_token)  # type: ignore[attr-defined]
        assert Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {access}").status_code == 200
        # Positive controls: before the erasure a refresh and a password login work. The refresh uses its
        # own token, because a refresh can rotate the token that it gets.
        assert _refresh(str(RefreshToken.for_user(holder))).status_code == 200
        assert "access" in _login(HOLDER_EMAIL, "old-password").json()
        delete_user_account(str(holder.pk))
        return RevelUser.objects.get(pk=holder.pk), access, str(refresh)

    def test_an_old_access_token_gets_401(self, erased: tuple[RevelUser, str, str]) -> None:
        _user, access, _refresh = erased

        assert Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {access}").status_code == 401

    def test_an_old_refresh_token_gets_401(self, erased: tuple[RevelUser, str, str]) -> None:
        _user, _access, refresh = erased

        assert _refresh(refresh).status_code == 401

    @pytest.mark.parametrize("username", [HOLDER_EMAIL, "placeholder"])
    def test_a_password_login_fails(self, erased: tuple[RevelUser, str, str], username: str) -> None:
        user, _access, _refresh = erased
        login = user.username if username == "placeholder" else username

        response = _login(login, "old-password")

        assert response.status_code in (400, 401, 403)
        assert "access" not in response.json()

    def test_oidc_with_the_old_email_makes_a_new_account(self, erased: tuple[RevelUser, str, str]) -> None:
        user, _access, _refresh = erased
        assert not ExternalIdentity.objects.filter(user=user).exists()

        fresh = oidc._resolve_user(
            GOOGLE,
            OIDCClaims(sub="sub-1", email=HOLDER_EMAIL, email_verified=True, given_name="Ada", family_name="L"),
        )

        assert fresh.pk != user.pk
        assert not Ticket.objects.filter(user=fresh).exists()

    def test_a_queued_data_export_is_skipped(self, erased: tuple[RevelUser, str, str]) -> None:
        user, _access, _refresh = erased

        with patch("accounts.service.gdpr.generate_user_data_export") as export:
            generate_user_data_export(str(user.pk))

        export.assert_not_called()


def test_a_queued_data_export_is_skipped_while_the_deletion_is_pending(holder: RevelUser) -> None:
    RevelUser.objects.filter(pk=holder.pk).update(is_active=False, deletion_requested_at=timezone.now())

    with (
        patch("accounts.service.gdpr.generate_user_data_export") as export,
        patch("accounts.tasks.gdpr.send_email") as send,
    ):
        generate_user_data_export(str(holder.pk))

    export.assert_not_called()
    send.assert_not_called()
    assert not UserDataExport.objects.filter(user=holder).exists()


def test_a_queued_data_export_of_an_account_that_is_gone_is_skipped() -> None:
    with patch("accounts.service.gdpr.generate_user_data_export") as export:
        generate_user_data_export(str(uuid.uuid4()))

    export.assert_not_called()


_ERASE = "accounts.service.account_erasure.erase_account_keeping_holdings"


# The mark that the last self-service confirm queued its run with, for each account (None: no mark).
_QUEUED_MARKS: dict[uuid.UUID, str | None] = {}


def _confirm(user: RevelUser, capture: t.Any) -> None:
    """Run the self-service confirm and its on-commit dispatch, with the task itself not queued."""
    with patch("accounts.tasks.delete_user_account.delay") as task, capture(execute=True):
        account_service.confirm_account_deletion(_deletion_token(user))
    _QUEUED_MARKS[user.pk] = task.call_args.kwargs["deletion_mark"]


def _queued_run(user: RevelUser, **kwargs: t.Any) -> dict[str, t.Any]:
    """The task kwargs of the run that the self-service confirm queued (an admin run has no mark)."""
    return {"deletion_mark": _QUEUED_MARKS.get(user.pk), **kwargs}


def _fail_the_deletion(user: RevelUser, *, marked: bool = True) -> None:
    """Run the deletion task once more, failing for good.

    The run has the mark of the confirm (if any); ``marked=False`` is a run without a mark (an admin or ban run).
    """
    with patch(_ERASE, side_effect=RuntimeError("x")), pytest.raises(RuntimeError):
        delete_user_account.apply(args=(str(user.pk),), kwargs=_queued_run(user) if marked else {})


def _the_queued_run_stops(user: RevelUser) -> None:
    """The run of the confirm finds its mark gone: it ends without work and without an error."""
    with patch(_ERASE) as erase, patch("accounts.tasks.gdpr.logger") as log:
        delete_user_account.apply(args=(str(user.pk),), kwargs=_queued_run(user))
    erase.assert_not_called()
    log.info.assert_called_once_with("account_deletion_skipped_mark_changed", user_id=str(user.pk))
    log.error.assert_not_called()


def _model_admin() -> t.Any:
    from django.contrib import admin as django_admin

    from accounts.admin.user import RevelUserAdmin

    return RevelUserAdmin(RevelUser, django_admin.site)


def _request(user: RevelUser) -> t.Any:
    return type("Request", (), {"user": user})()


def _admin_edit(
    user: RevelUser, admin_user: RevelUser, *, shown_active: bool | None, submit_active: bool, bio: str = "edited"
) -> None:
    """Post the admin change form of ``user`` for ``is_active`` and ``bio``, as Django does.

    ``shown_active`` is the value the form showed (its hidden initial input); None posts without it.
    """
    model_admin, request = _model_admin(), _request(admin_user)
    at_submit = RevelUser.objects.get(pk=user.pk)  # Django reloads the row on POST
    form_class = model_admin.get_form(request, at_submit, change=True, fields=["is_active", "bio"])
    data = {"bio": bio}
    if submit_active:
        data["is_active"] = "on"
    if shown_active is not None:
        data[form_class(instance=at_submit)["is_active"].html_initial_name] = str(shown_active)
    form = form_class(data, instance=at_submit)
    assert form.is_valid(), form.errors
    model_admin.save_model(request, form.save(commit=False), form, True)


def _is_active(user: RevelUser) -> bool:
    return RevelUser.objects.values_list("is_active", flat=True).get(pk=user.pk)


class TestFinalFailureOfTheDeletionTask:
    """The request deactivated the account; a deletion that fails for good must not lock the user out."""

    @pytest.fixture
    def queued(
        self, holder: RevelUser, ended_event_factory: Factory, django_capture_on_commit_callbacks: t.Any
    ) -> RevelUser:
        """An account that the self-service confirm deactivated and marked, with a kept ticket."""
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        _confirm(holder, django_capture_on_commit_callbacks)
        return holder

    def test_the_confirm_path_marks_the_account(self, queued: RevelUser) -> None:
        queued.refresh_from_db()
        assert not queued.is_active
        assert queued.deletion_requested_at is not None

    def test_an_account_that_was_already_inactive_gets_no_mark_and_stays_inactive(
        self, holder: RevelUser, ended_event_factory: Factory, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """An admin or the unverified sweep deactivated it before the confirm: a failure must not undo that."""
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        RevelUser.objects.filter(pk=holder.pk).update(is_active=False)
        _confirm(holder, django_capture_on_commit_callbacks)

        _fail_the_deletion(holder)

        holder.refresh_from_db()
        assert not holder.is_active
        assert holder.deletion_requested_at is None

    def test_an_admin_queued_failure_leaves_the_account_inactive(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        """An admin deletes accounts that may be inactive for another reason (a ban): no mark, no reactivation."""
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        RevelUser.objects.filter(pk=holder.pk).update(is_active=False)

        _fail_the_deletion(holder)

        assert not _is_active(holder)

    def test_an_admin_delete_of_a_pending_account_clears_the_mark(
        self, queued: RevelUser, admin_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The admin run fails, and the self-service run stops without work: neither gives access back."""
        with patch("accounts.tasks.gdpr.delete_user_account"), django_capture_on_commit_callbacks(execute=True):
            _model_admin().delete_model(_request(admin_user), RevelUser.objects.get(pk=queued.pk))

        _fail_the_deletion(queued, marked=False)  # the admin run
        _the_queued_run_stops(queued)  # the self-service run

        queued.refresh_from_db()
        assert not queued.is_active
        assert queued.deletion_requested_at is None

    def test_an_admin_who_unticks_is_active_clears_the_mark(self, queued: RevelUser, admin_user: RevelUser) -> None:
        """The form showed the account active (opened before the confirm); the admin unticks it."""
        _admin_edit(queued, admin_user, shown_active=True, submit_active=False)

        _the_queued_run_stops(queued)  # the admin decided: the account stays, inactive

        queued.refresh_from_db()
        assert not queued.is_active
        assert queued.deletion_requested_at is None
        assert queued.erased_at is None

    def test_another_admin_change_keeps_the_mark(self, queued: RevelUser, admin_user: RevelUser) -> None:
        _admin_edit(queued, admin_user, shown_active=False, submit_active=False)

        assert RevelUser.objects.get(pk=queued.pk).deletion_requested_at is not None
        _fail_the_deletion(queued)

        assert _is_active(queued)

    def test_a_post_without_the_shown_value_is_compared_with_the_row(
        self, holder: RevelUser, admin_user: RevelUser
    ) -> None:
        """A scripted post has no hidden initial input: unticking an active account still deactivates it."""
        _admin_edit(holder, admin_user, shown_active=None, submit_active=False)

        assert not _is_active(holder)

    def test_a_post_without_the_shown_value_that_ticks_a_pending_account_reactivates_it(
        self, queued: RevelUser, admin_user: RevelUser
    ) -> None:
        _admin_edit(queued, admin_user, shown_active=None, submit_active=True)

        queued.refresh_from_db()
        assert queued.is_active
        assert queued.deletion_requested_at is None

    def test_a_post_without_the_shown_value_that_leaves_is_active_keeps_the_mark(
        self, queued: RevelUser, admin_user: RevelUser
    ) -> None:
        _admin_edit(queued, admin_user, shown_active=None, submit_active=False)

        queued.refresh_from_db()
        assert not queued.is_active
        assert queued.deletion_requested_at is not None
        _fail_the_deletion(queued)
        assert _is_active(queued)

    def test_a_ban_of_a_pending_account_clears_the_mark(self, queued: RevelUser) -> None:
        """A Telegram ban does not match the email-ban check of the final failure: the mark must go."""
        from accounts.service.global_ban_service import deactivate_user_for_ban

        deactivate_user_for_ban(RevelUser.objects.get(pk=queued.pk), "spam")

        _the_queued_run_stops(queued)  # the self-service run
        _fail_the_deletion(queued, marked=False)  # the run that the ban queues

        queued.refresh_from_db()
        assert not queued.is_active
        assert queued.deletion_requested_at is None

    def test_a_ban_that_read_the_account_before_a_restore_still_deactivates_it(self, queued: RevelUser) -> None:
        """The restore commits between the ban's read of the account and its own write."""
        from accounts.service.account_erasure import restore_access_after_failed_deletion
        from accounts.service.global_ban_service import deactivate_user_for_ban

        read_by_the_ban = RevelUser.objects.get(pk=queued.pk)  # inactive and marked
        assert restore_access_after_failed_deletion(str(queued.pk), read_by_the_ban.deletion_requested_at)
        with patch("accounts.service.global_ban_service.blacklist_user_tokens") as blacklist:
            deactivate_user_for_ban(read_by_the_ban, "spam")

        queued.refresh_from_db()
        assert not queued.is_active
        assert queued.deletion_requested_at is None
        blacklist.assert_called_once_with(read_by_the_ban)

    def test_a_globally_banned_account_stays_inactive(self, queued: RevelUser) -> None:
        from accounts.models import GlobalBan
        from accounts.utils.email_normalization import normalize_email_for_matching

        # bulk_create: no post_save, so only the check of the final failure stops the reactivation.
        GlobalBan.objects.bulk_create(
            [
                GlobalBan(
                    ban_type=GlobalBan.BanType.EMAIL,
                    value=queued.email,
                    normalized_value=normalize_email_for_matching(queued.email),
                )
            ]
        )

        _fail_the_deletion(queued)

        assert not _is_active(queued)

    def test_an_erasure_that_commits_during_the_failure_handling_is_not_undone(self, queued: RevelUser) -> None:
        """The account is erased after the failure handler read it (here: during its ban check)."""
        from accounts.utils.email_normalization import normalize_email_for_matching

        def erase_meanwhile(email: str) -> str:
            RevelUser.objects.filter(pk=queued.pk).update(erased_at=timezone.now())
            return normalize_email_for_matching(email)

        with patch("accounts.service.global_ban_service.normalize_email_for_matching", side_effect=erase_meanwhile):
            _fail_the_deletion(queued)

        queued.refresh_from_db()
        assert queued.erased_at is not None
        assert not queued.is_active

    def test_a_deferral_with_retries_left_does_not_reactivate(self, queued: RevelUser) -> None:
        from accounts.exceptions import ReferralPayoutInFlightError

        with (
            patch("accounts.tasks.gdpr.cleanup_referral_data", side_effect=ReferralPayoutInFlightError()),
            patch.object(delete_user_account, "retry", side_effect=ReferralPayoutInFlightError()) as retry,
            patch("accounts.tasks.gdpr.logger") as log,
            pytest.raises(ReferralPayoutInFlightError),
        ):
            delete_user_account.apply(args=(str(queued.pk),), kwargs=_queued_run(queued), retries=0)

        retry.assert_called_once()  # the deferral asks for a retry; it does not only pass the error on
        assert not _is_active(queued)
        log.error.assert_not_called()

    def test_a_failed_force_delete_of_an_erased_account_leaves_it_inactive(self, queued: RevelUser) -> None:
        delete_user_account.apply(args=(str(queued.pk),))  # erased in place
        mark = timezone.now()
        RevelUser.objects.filter(pk=queued.pk).update(deletion_requested_at=mark)  # only the erasure guards
        with (
            patch("accounts.service.gdpr.purge_user_history", side_effect=RuntimeError("x")),
            pytest.raises(RuntimeError),
        ):
            delete_user_account.apply(
                args=(str(queued.pk),), kwargs={"force_full_delete": True, "deletion_mark": mark.isoformat()}
            )

        queued.refresh_from_db()
        assert queued.erased_at is not None
        assert not queued.is_active

    def test_a_failed_account_lookup_makes_the_account_active_again(self, queued: RevelUser) -> None:
        """The first lookup of the task fails (a database error); the recovery update still runs."""
        from django.db import DatabaseError

        real_filter = RevelUser.objects.filter
        failed = False

        def fail_first_lookup(*args: t.Any, **kwargs: t.Any) -> t.Any:
            nonlocal failed
            if not failed:
                failed = True
                raise DatabaseError("database unavailable")
            return real_filter(*args, **kwargs)

        with patch.object(RevelUser.objects, "filter", side_effect=fail_first_lookup), pytest.raises(DatabaseError):
            delete_user_account.apply(args=(str(queued.pk),), kwargs=_queued_run(queued))

        queued.refresh_from_db()
        assert failed
        assert queued.is_active
        assert queued.deletion_requested_at is None

    def test_an_error_makes_the_account_active_again_and_logs_the_id_only(self, queued: RevelUser) -> None:
        with patch("accounts.tasks.gdpr.logger") as log:
            _fail_the_deletion(queued)  # eager tasks propagate in tests

        queued.refresh_from_db()
        assert queued.is_active
        assert queued.erased_at is None
        assert queued.deletion_requested_at is None
        log.error.assert_called_once_with("account_deletion_failed", user_id=str(queued.pk))

    def test_a_payout_still_in_flight_after_the_last_retry_makes_it_active_again(self, queued: RevelUser) -> None:
        from accounts.exceptions import ReferralPayoutInFlightError

        with (
            patch("accounts.tasks.gdpr.cleanup_referral_data", side_effect=ReferralPayoutInFlightError()),
            pytest.raises(ReferralPayoutInFlightError),
        ):
            delete_user_account.apply(
                args=(str(queued.pk),), kwargs=_queued_run(queued), retries=delete_user_account.max_retries
            )

        assert _is_active(queued)

    def test_a_successful_run_leaves_the_account_erased_inactive_and_unmarked(self, queued: RevelUser) -> None:
        delete_user_account.apply(args=(str(queued.pk),))

        queued.refresh_from_db()
        assert queued.erased_at is not None
        assert not queued.is_active
        assert queued.deletion_requested_at is None

    def test_a_direct_call_by_the_sweep_leaves_the_account_inactive(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        """The unverified sweep deactivated the account itself (no mark) and retries it on its next run."""
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        RevelUser.objects.filter(pk=holder.pk).update(is_active=False)
        with patch(_ERASE, side_effect=RuntimeError("x")), pytest.raises(RuntimeError):
            delete_user_account(str(holder.pk))

        assert not _is_active(holder)


class TestDispatchOfTheSelfServiceDeletion:
    def test_a_broker_failure_logs_the_id_and_keeps_the_account_pending(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The request answered that the deletion will run: no access comes back, and the sweep queues it again."""
        with (
            patch("accounts.tasks.delete_user_account.delay", side_effect=ConnectionError("broker down")),
            patch("accounts.service.account.logger") as log,
            django_capture_on_commit_callbacks(execute=True),
        ):
            account_service.confirm_account_deletion(_deletion_token(holder))

        holder.refresh_from_db()
        assert not holder.is_active
        assert holder.deletion_requested_at is not None
        log.error.assert_called_once_with("account_deletion_dispatch_failed", user_id=str(holder.pk), exc_info=True)

    def test_a_dispatch_that_failed_is_queued_again_by_the_sweep_with_a_new_mark(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with (
            patch("accounts.tasks.delete_user_account.delay", side_effect=ConnectionError("broker down")),
            django_capture_on_commit_callbacks(execute=True),
        ):
            account_service.confirm_account_deletion(_deletion_token(holder))
        RevelUser.objects.filter(pk=holder.pk).update(
            deletion_requested_at=timezone.now() - STALE_ACCOUNT_DELETION_AFTER - timedelta(minutes=1)
        )

        with patch("accounts.tasks.gdpr.delete_user_account.delay") as task:
            assert requeue_stale_account_deletions() == {"requeued": 1}

        holder.refresh_from_db()
        assert not holder.is_active
        assert holder.deletion_requested_at is not None
        task.assert_called_once_with(str(holder.pk), deletion_mark=holder.deletion_requested_at.isoformat())

    def test_a_successful_dispatch_queues_the_task_once(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with (
            patch("accounts.tasks.delete_user_account.delay") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            account_service.confirm_account_deletion(_deletion_token(holder))

        holder.refresh_from_db()
        assert not holder.is_active
        assert holder.deletion_requested_at is not None
        task.assert_called_once_with(str(holder.pk), deletion_mark=holder.deletion_requested_at.isoformat())

    def test_a_second_confirmation_while_one_is_pending_queues_nothing(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with (
            patch("accounts.tasks.delete_user_account.delay") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            account_service.confirm_account_deletion(_deletion_token(holder))
            account_service.confirm_account_deletion(_deletion_token(holder))

        mark = RevelUser.objects.values_list("deletion_requested_at", flat=True).get(pk=holder.pk)
        assert mark is not None
        task.assert_called_once_with(str(holder.pk), deletion_mark=mark.isoformat())

    def test_a_second_confirmation_cannot_give_access_back_while_the_first_run_is_queued(
        self, holder: RevelUser, ended_event_factory: Factory, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """Two valid confirmations; the broker fails for the second one; the first run is still queued and runs."""
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        with (
            patch(
                "accounts.tasks.delete_user_account.delay", side_effect=[None, ConnectionError("broker down")]
            ) as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            account_service.confirm_account_deletion(_deletion_token(holder))
            account_service.confirm_account_deletion(_deletion_token(holder))

        holder.refresh_from_db()
        assert not holder.is_active
        assert holder.deletion_requested_at is not None
        task.assert_called_once()  # the second confirmation queued nothing
        first_run = task.call_args_list[0]
        assert first_run.kwargs == {"deletion_mark": holder.deletion_requested_at.isoformat()}

        # The queued first run, as it was published: with its mark, so the check on the mark runs.
        delete_user_account.apply(args=first_run.args, kwargs=dict(first_run.kwargs))

        holder.refresh_from_db()
        assert holder.erased_at is not None
        assert not holder.is_active

    def test_each_confirmation_of_an_account_that_has_no_mark_still_queues_a_run(
        self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """An admin deactivated the account: no mark, so no run can give the access back; the deletion still runs."""
        RevelUser.objects.filter(pk=holder.pk).update(is_active=False)
        with (
            patch("accounts.tasks.delete_user_account.delay") as task,
            django_capture_on_commit_callbacks(execute=True),
        ):
            account_service.confirm_account_deletion(_deletion_token(holder))
            account_service.confirm_account_deletion(_deletion_token(holder))

        assert task.call_args_list == [call(str(holder.pk), deletion_mark=None)] * 2

    def test_a_run_without_a_mark_cannot_clear_the_mark_of_a_later_confirmation(
        self,
        holder: RevelUser,
        admin_user: RevelUser,
        ended_event_factory: Factory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """Confirmed while inactive (no mark), reactivated by an admin, confirmed again; then the first run fails."""
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        RevelUser.objects.filter(pk=holder.pk).update(is_active=False)
        _confirm(holder, django_capture_on_commit_callbacks)
        first_run = _queued_run(holder)
        assert first_run == {"deletion_mark": None}
        _admin_edit(holder, admin_user, shown_active=False, submit_active=True)
        assert _is_active(holder)
        _confirm(holder, django_capture_on_commit_callbacks)
        second_mark = RevelUser.objects.values_list("deletion_requested_at", flat=True).get(pk=holder.pk)
        assert second_mark is not None

        with patch(_ERASE, side_effect=RuntimeError("x")), pytest.raises(RuntimeError):
            delete_user_account.apply(args=(str(holder.pk),), kwargs=first_run)

        holder.refresh_from_db()
        assert not holder.is_active  # the second run is still queued
        assert holder.deletion_requested_at == second_mark
        _fail_the_deletion(holder)  # the run of the second confirmation fails for good: its mark matches
        assert _is_active(holder)

    def test_a_run_with_an_older_mark_cannot_clear_a_newer_mark(
        self, holder: RevelUser, ended_event_factory: Factory, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The sweep gave the account a new mark and a new run; then the old run starts, and stops."""
        make_ticket(holder, ended_event_factory(-timedelta(days=5)))
        with freeze_time(timezone.now()) as clock:
            _confirm(holder, django_capture_on_commit_callbacks)
            old_run = _queued_run(holder)
            # The clock moves, the mark does not: until the sweep, the first run is the run of the stored mark.
            clock.tick(STALE_ACCOUNT_DELETION_AFTER + timedelta(minutes=1))
            confirmed = RevelUser.objects.values_list("deletion_requested_at", flat=True).get(pk=holder.pk)
            assert confirmed is not None and old_run == {"deletion_mark": confirmed.isoformat()}
            with patch("accounts.tasks.gdpr.delete_user_account.delay") as task:
                requeue_stale_account_deletions()
        new_run = dict(task.call_args.kwargs)
        stored = RevelUser.objects.values_list("deletion_requested_at", flat=True).get(pk=holder.pk)
        assert stored is not None and stored != confirmed  # the sweep wrote a new mark
        assert new_run == {"deletion_mark": stored.isoformat()} != old_run

        with patch(_ERASE) as erase:
            delete_user_account.apply(args=(str(holder.pk),), kwargs=old_run)  # stops: its mark is not on the row
        erase.assert_not_called()
        assert not _is_active(holder)
        assert RevelUser.objects.values_list("deletion_requested_at", flat=True).get(pk=holder.pk) == stored

        with patch(_ERASE, side_effect=RuntimeError("x")), pytest.raises(RuntimeError):
            delete_user_account.apply(args=(str(holder.pk),), kwargs=new_run)
        assert _is_active(holder)


class TestLinksSentBeforeTheErasure:
    """A link mailed before the deletion must not bring the erased account back."""

    @pytest.fixture
    def holder_with_ticket(self, holder: RevelUser, ended_event_factory: Factory) -> RevelUser:
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        return holder

    def test_a_password_reset_link_finds_no_account(self, holder_with_ticket: RevelUser) -> None:
        token = account_service.create_password_reset_token(holder_with_ticket)
        delete_user_account(str(holder_with_ticket.pk))

        with pytest.raises(Http404):
            account_service.reset_password(token, "A-new-password-123!")

        erased = RevelUser.objects.get(pk=holder_with_ticket.pk)
        assert not erased.is_active
        assert not erased.email_verified
        assert not erased.has_usable_password()

    def test_an_email_verification_link_is_refused(self, holder_with_ticket: RevelUser) -> None:
        token = account_service.create_verification_token(holder_with_ticket)
        delete_user_account(str(holder_with_ticket.pk))

        with pytest.raises(HttpError):
            account_service.verify_email(token)

        assert not RevelUser.objects.get(pk=holder_with_ticket.pk).is_active


class TestLinksWhileTheDeletionIsPending:
    """A link or login must not reactivate an account whose self-service deletion is still pending."""

    @pytest.fixture
    def pending(self, holder: RevelUser, django_capture_on_commit_callbacks: t.Any) -> RevelUser:
        """An unverified account that confirmed its deletion; the task has not run yet."""
        RevelUser.objects.filter(pk=holder.pk).update(email_verified=False)
        holder.refresh_from_db()
        _confirm(holder, django_capture_on_commit_callbacks)
        holder.refresh_from_db()
        assert holder.deletion_requested_at is not None
        return holder

    def test_an_email_verification_link_gets_the_answer_for_a_missing_account(self, pending: RevelUser) -> None:
        token = account_service.create_verification_token(pending)
        missing = RevelUser(id=uuid.uuid4(), email="gone@example.com")
        missing_token = account_service.create_verification_token(missing)

        with pytest.raises(HttpError) as refused:
            account_service.verify_email(token)
        with pytest.raises(HttpError) as gone:
            account_service.verify_email(missing_token)

        assert (refused.value.status_code, str(refused.value)) == (gone.value.status_code, str(gone.value))
        pending.refresh_from_db()
        assert not pending.is_active
        assert not pending.email_verified
        assert pending.deletion_requested_at is not None

    @pytest.mark.parametrize("verified", [False, True])
    def test_a_password_reset_link_finds_no_account(self, pending: RevelUser, verified: bool) -> None:
        RevelUser.objects.filter(pk=pending.pk).update(email_verified=verified)
        token = account_service.create_password_reset_token(pending)

        with pytest.raises(Http404):
            account_service.reset_password(token, "A-new-password-123!")

        pending.refresh_from_db()
        assert not pending.is_active
        assert not pending.check_password("A-new-password-123!")

    def test_an_oidc_login_does_not_convert_or_reactivate_a_pending_guest(self, pending: RevelUser) -> None:
        from accounts.exceptions import OIDCLoginError

        RevelUser.objects.filter(pk=pending.pk).update(guest=True)

        with pytest.raises(OIDCLoginError):
            oidc._resolve_user(
                GOOGLE,
                OIDCClaims(sub="sub-2", email=HOLDER_EMAIL, email_verified=True, given_name="Ada", family_name="L"),
            )

        pending.refresh_from_db()
        assert pending.guest
        assert not pending.is_active

    def test_an_admin_who_ticks_is_active_clears_the_mark(self, pending: RevelUser, admin_user: RevelUser) -> None:
        """The admin decides: an operator reactivates by hand, and a later failure changes nothing."""
        _admin_edit(pending, admin_user, shown_active=False, submit_active=True)

        pending.refresh_from_db()
        assert pending.is_active
        assert pending.deletion_requested_at is None

    def test_a_deletion_confirmed_during_a_password_reset_is_seen_under_the_lock(self, holder: RevelUser) -> None:
        """The confirm commits after the first lookup of the reset; the locked re-read refuses the account."""
        token = account_service.create_password_reset_token(holder)

        def confirm_meanwhile(email: str) -> bool:
            RevelUser.objects.filter(pk=holder.pk).update(is_active=False, deletion_requested_at=timezone.now())
            return False

        with (
            patch("accounts.service.global_ban_service.is_email_globally_banned", side_effect=confirm_meanwhile),
            pytest.raises(Http404),
        ):
            account_service.reset_password(token, "A-new-password-123!")

        holder.refresh_from_db()
        assert not holder.is_active
        assert not holder.check_password("A-new-password-123!")

    def test_no_new_link_is_sent_to_a_pending_account(
        self, pending: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The emails are dispatched on commit, so the callbacks run before the check."""
        with (
            patch("accounts.tasks.send_account_email.delay") as send,
            django_capture_on_commit_callbacks(execute=True),
        ):
            account_service.resend_verification_email(pending.email)
            assert account_service.request_password_reset(pending.email) is None

        send.assert_not_called()

    @pytest.mark.parametrize("field", ["deletion_requested_at", "erased_at", None])
    def test_a_repeat_signup_on_a_pending_guest_sends_no_activation_link_and_answers_the_same(
        self, django_user_model: type[RevelUser], field: str | None
    ) -> None:
        """``field=None`` is the control: a guest that is not being deleted gets the link and the same answer."""
        guest = django_user_model.objects.create_user(username="guest@example.com", email="guest@example.com")
        RevelUser.objects.filter(pk=guest.pk).update(guest=True, **({field: timezone.now()} if field else {}))
        payload = schema.RegisterUserSchema(
            email=guest.email,
            password1="Strong-password-123!",
            password2="Strong-password-123!",
            first_name="A",
            last_name="B",
            accept_toc_and_privacy=True,
        )

        with patch("accounts.tasks.send_account_email.delay") as send, pytest.raises(HttpError) as refused:
            account_service.register_user(payload)

        assert (refused.value.status_code, str(refused.value)) == (400, "A user with this email already exists.")
        assert send.call_count == (0 if field else 1)

    def test_the_admin_verification_action_skips_a_pending_account(
        self, pending: RevelUser, admin_user: RevelUser
    ) -> None:
        with (
            patch("accounts.admin.user.send_verification_email_for_user") as send,
            patch.object(_model_admin().__class__, "message_user"),
        ):
            _model_admin().send_verification_email(_request(admin_user), RevelUser.objects.filter(pk=pending.pk))

        send.assert_not_called()

    def test_an_admin_form_opened_before_the_confirm_does_not_write_is_active_back(
        self, holder: RevelUser, admin_user: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """The form showed the account active; the confirm commits; then the admin posts an unrelated edit."""
        _confirm(holder, django_capture_on_commit_callbacks)
        _admin_edit(holder, admin_user, shown_active=True, submit_active=True)

        holder.refresh_from_db()
        assert not holder.is_active
        assert holder.deletion_requested_at is not None
        assert holder.bio == "edited"
