"""Deleting an account that holds issued tickets or passes erases it in place and keeps them (#1476)."""

import typing as t
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import stripe
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import connection
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from ninja_jwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from ninja_jwt.tokens import RefreshToken

from accounts.models import ExternalIdentity, RevelUser, UserDataExport
from accounts.service import account_erasure
from accounts.tasks import delete_user_account
from common.email_backend import ERASED_EMAIL_DOMAIN
from conftest import RevelUserFactory
from events.models import (
    CustomerProfile,
    Event,
    EventRSVP,
    HeldSeriesPass,
    Organization,
    OrganizationMember,
    Payment,
    Refund,
    SeriesPass,
    Ticket,
    TicketTier,
)
from events.service.account_deletion_service import kept_holdings
from events.service.stripe_webhooks import StripeEventHandler
from events.tests.test_account_erasure.conftest import HOLDER_EMAIL, make_payment, make_ticket
from notifications.models import Notification, NotificationPreference

pytestmark = pytest.mark.django_db

Factory = t.Callable[[timedelta], Event]


def _erase(user: RevelUser) -> RevelUser:
    delete_user_account(str(user.pk))
    return RevelUser.objects.get(pk=user.pk)


class TestKeptTicket:
    def test_the_account_is_anonymised_and_the_ticket_payment_and_refund_stay(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=7)))
        payment = make_payment(ticket)
        refund = Refund.objects.create(
            payment=payment, amount=Decimal("1.00"), currency="EUR", source=Refund.Source.ORGANIZER_API
        )
        TicketTier.objects.filter(pk=ticket.tier_id).update(quantity_sold=1)
        holder.set_password("secret")
        holder.last_login = timezone.now()
        holder.save()

        erased = _erase(holder)

        assert erased.erased_at is not None
        assert not erased.is_active
        assert erased.email == erased.username == f"erased-{holder.pk.hex}@{ERASED_EMAIL_DOMAIN}"
        assert (erased.first_name, erased.last_name, erased.preferred_name, erased.pronouns, erased.bio) == (("",) * 5)
        assert erased.phone_number is None
        assert not erased.has_usable_password()
        assert erased.last_login is None
        assert not erased.email_verified
        assert erased.get_display_name() == "Deleted user"
        ticket.refresh_from_db()
        assert ticket.user_id == holder.pk
        assert ticket.status == Ticket.TicketStatus.ACTIVE
        assert ticket.holder_email == HOLDER_EMAIL
        assert ticket.guest_name == "Ada L"
        assert Payment.objects.filter(pk=payment.pk).exists()
        assert Refund.objects.filter(pk=refund.pk).exists()
        # The tier count and the event's ticket rows agree: the place is still taken by a live ticket.
        assert TicketTier.objects.get(pk=ticket.tier_id).quantity_sold == 1
        assert Ticket.objects.filter(tier_id=ticket.tier_id).count() == 1

    def test_a_guest_name_for_another_person_is_kept(self, holder: RevelUser, ended_event_factory: Factory) -> None:
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=1)), guest_name="Charles B")

        _erase(holder)

        ticket.refresh_from_db()
        assert (ticket.guest_name, ticket.holder_email) == ("Charles B", HOLDER_EMAIL)

    @pytest.mark.parametrize("status", [Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CHECKED_IN])
    def test_an_event_that_ended_119_days_ago_keeps_the_ticket(
        self, holder: RevelUser, ended_event_factory: Factory, status: str
    ) -> None:
        ticket = make_ticket(holder, ended_event_factory(timedelta(days=119)), status=status)

        _erase(holder)

        assert Ticket.objects.filter(pk=ticket.pk).exists()

    def test_an_event_that_ended_121_days_ago_is_a_full_delete(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        ticket = make_ticket(holder, ended_event_factory(timedelta(days=121)))

        delete_user_account(str(holder.pk))

        assert not RevelUser.objects.filter(pk=holder.pk).exists()
        assert not Ticket.objects.filter(pk=ticket.pk).exists()

    def test_the_cutoff_compares_datetimes_not_dates(self, holder: RevelUser, ended_event_factory: Factory) -> None:
        """An event that ended one hour past the 120-day cutoff is out of scope, even on the same date."""
        make_ticket(holder, ended_event_factory(timedelta(days=120, hours=1)))
        inside = make_ticket(holder, ended_event_factory(timedelta(days=119, hours=23)))

        assert kept_holdings(holder, timezone.now()).ticket_ids == {inside.pk}


class TestRowsThatGo:
    def test_cancelled_and_pending_tickets_go_and_pending_gives_back_its_place(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        event = ended_event_factory(-timedelta(days=3))
        kept = make_ticket(holder, event)
        cancelled = make_ticket(holder, event, status=Ticket.TicketStatus.CANCELLED)
        pending = make_ticket(holder, event, status=Ticket.TicketStatus.PENDING)
        make_payment(pending, status=Payment.PaymentStatus.PENDING)
        TicketTier.objects.filter(pk=pending.tier_id).update(quantity_sold=1)

        _erase(holder)

        assert list(Ticket.objects.filter(user=holder).values_list("pk", flat=True)) == [kept.pk]
        assert not Ticket.objects.filter(pk__in=[cancelled.pk, pending.pk]).exists()
        assert not Payment.objects.filter(ticket_id=pending.pk).exists()
        assert TicketTier.objects.get(pk=pending.tier_id).quantity_sold == 0

    def test_only_cancelled_tickets_is_a_full_delete(self, holder: RevelUser, ended_event_factory: Factory) -> None:
        make_ticket(holder, ended_event_factory(-timedelta(days=3)), status=Ticket.TicketStatus.CANCELLED)

        delete_user_account(str(holder.pk))

        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    def test_the_other_rows_get_the_outcome_of_a_delete(
        self,
        holder: RevelUser,
        member_user: RevelUser,
        organization: Organization,
        ended_event_factory: Factory,
    ) -> None:
        event = ended_event_factory(-timedelta(days=3))
        make_ticket(holder, event)
        rsvp = EventRSVP.objects.create(event=event, user=holder, status=EventRSVP.RsvpStatus.YES)
        membership = OrganizationMember.objects.create(organization=organization, user=holder)
        identity = ExternalIdentity.objects.create(user=holder, provider="google", subject="sub-1")
        notification = Notification.objects.create(user=holder, notification_type="event_reminder", context={"k": "v"})
        other = make_ticket(member_user, event, status=Ticket.TicketStatus.CHECKED_IN, checked_in_by=holder)
        refresh = RefreshToken.for_user(holder)
        profile = CustomerProfile.objects.create(user=holder, organization=organization, stripe_customer_id="cus_1")
        assert profile.history.exists()

        _erase(holder)

        assert not EventRSVP.objects.filter(pk=rsvp.pk).exists()
        assert not OrganizationMember.objects.filter(pk=membership.pk).exists()
        assert not ExternalIdentity.objects.filter(pk=identity.pk).exists()
        assert not Notification.objects.filter(pk=notification.pk).exists()
        assert not CustomerProfile.objects.filter(pk=profile.pk).exists()
        assert not CustomerProfile.history.filter(user_id=holder.pk).exists()
        other.refresh_from_db()
        assert other.checked_in_by_id is None
        outstanding = OutstandingToken.objects.get(jti=refresh["jti"])
        assert outstanding.user_id is None
        assert BlacklistedToken.objects.filter(token=outstanding).exists()
        # Kept and not silenced: the event-cancel audience skips silenced accounts (the dispatcher gates the rest).
        prefs = NotificationPreference.objects.get(user=holder)
        assert not prefs.silence_all_notifications


class TestSeriesPass:
    def test_an_active_pass_of_a_series_with_a_recent_event_is_kept_with_a_snapshot(
        self, holder: RevelUser, series_pass: SeriesPass, ended_event_factory: Factory
    ) -> None:
        ended_event_factory(timedelta(days=30))
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass, user=holder, status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE, price_paid=0
        )

        _erase(holder)

        held.refresh_from_db()
        assert (held.holder_name, held.holder_email) == ("Ada L", HOLDER_EMAIL)

    def test_staff_see_the_pass_snapshot_in_the_holder_list_and_the_cancel_alert(
        self, holder: RevelUser, series_pass: SeriesPass
    ) -> None:
        from events.schema import HeldSeriesPassAdminSchema
        from notifications.signals import series_pass as pass_signals

        held = HeldSeriesPass.objects.create(
            series_pass=series_pass, user=holder, status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE, price_paid=0
        )
        _erase(holder)
        held = HeldSeriesPass.objects.select_related("user").get(pk=held.pk)

        row = HeldSeriesPassAdminSchema.from_orm(held)
        with patch.object(pass_signals, "notify_org_staff") as notify:
            pass_signals.send_series_pass_cancelled(held.pk, Decimal("0"), 0, "")

        dumped = row.model_dump()
        assert (dumped["erased"], dumped["holder_name"], dumped["holder_email"]) == (True, "Ada L", HOLDER_EMAIL)
        staff_context = notify.call_args.kwargs["context"]
        assert (staff_context["holder_name"], staff_context["holder_email"]) == ("Ada L", HOLDER_EMAIL)

    def test_every_active_pass_is_kept_even_when_the_series_events_ended_long_ago(
        self, holder: RevelUser, series_pass: SeriesPass, event: Event, ended_event_factory: Factory
    ) -> None:
        """An ACTIVE pass can still cover future events; the retention job decides when it goes."""
        Event.objects.filter(pk=event.pk).update(
            start=timezone.now() - timedelta(days=200), end=timezone.now() - timedelta(days=199)
        )
        ended_event_factory(timedelta(days=150))
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass, user=holder, status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE, price_paid=0
        )

        erased = _erase(holder)

        assert erased.erased_at is not None
        assert HeldSeriesPass.objects.filter(pk=held.pk).exists()

    def test_an_active_pass_of_a_series_without_events_is_kept(
        self, holder: RevelUser, organization: Organization
    ) -> None:
        from events.models import EventSeries

        empty_series = EventSeries.objects.create(organization=organization, name="Next season", slug="next-season")
        season = SeriesPass.objects.create(
            event_series=empty_series,
            name="Next",
            price=Decimal("0"),
            pro_rata_discount=Decimal("0"),
            currency="EUR",
            payment_method=TicketTier.PaymentMethod.FREE,
        )
        held = HeldSeriesPass.objects.create(
            series_pass=season, user=holder, status=HeldSeriesPass.HeldSeriesPassStatus.ACTIVE, price_paid=0
        )

        erased = _erase(holder)

        assert erased.erased_at is not None
        held.refresh_from_db()
        assert held.holder_email == HOLDER_EMAIL

    def test_a_cancelled_pass_alone_is_a_full_delete(self, holder: RevelUser, series_pass: SeriesPass) -> None:
        HeldSeriesPass.objects.create(
            series_pass=series_pass, user=holder, status=HeldSeriesPass.HeldSeriesPassStatus.CANCELLED, price_paid=0
        )

        delete_user_account(str(holder.pk))

        assert not RevelUser.objects.filter(pk=holder.pk).exists()

    def test_the_cancelled_pass_of_a_kept_ticket_stays(
        self, holder: RevelUser, series_pass: SeriesPass, ended_event_factory: Factory
    ) -> None:
        """``Ticket.held_pass`` is RESTRICT, so the pass of a kept ticket must stay too."""
        held = HeldSeriesPass.objects.create(
            series_pass=series_pass, user=holder, status=HeldSeriesPass.HeldSeriesPassStatus.CANCELLED, price_paid=0
        )
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=2)), held_pass=held)

        _erase(holder)

        assert HeldSeriesPass.objects.filter(pk=held.pk).exists()
        assert Ticket.objects.filter(pk=ticket.pk).exists()


@pytest.fixture
def series_pass(event: Event) -> SeriesPass:
    assert event.event_series is not None
    return SeriesPass.objects.create(
        event_series=event.event_series,
        name="Season",
        price=Decimal("0"),
        pro_rata_discount=Decimal("0"),
        currency="EUR",
        payment_method=TicketTier.PaymentMethod.FREE,
    )


class TestKeptPayment:
    def test_the_stripe_payload_is_cleared_and_the_billing_snapshot_stays(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        """A late invoice or credit note needs the billing snapshot; the raw webhook payload is not needed."""
        payment = make_payment(make_ticket(holder, ended_event_factory(-timedelta(days=3))))
        snapshot = {"billing_name": "Ada L", "billing_email": HOLDER_EMAIL}
        Payment.objects.filter(pk=payment.pk).update(
            raw_response={"customer_details": {"email": HOLDER_EMAIL}}, buyer_billing_snapshot=snapshot
        )

        _erase(holder)

        payment.refresh_from_db()
        assert payment.raw_response == {}
        assert payment.buyer_billing_snapshot == snapshot

    @pytest.mark.parametrize("erase", [True, False], ids=["erased", "live"])
    def test_a_later_refund_event_does_not_store_the_buyer_details_again(
        self, holder: RevelUser, ended_event_factory: Factory, erase: bool
    ) -> None:
        """Stripe sends ``charge.refunded`` with the charge's billing details; ``live`` is the control."""
        payment = make_payment(make_ticket(holder, ended_event_factory(-timedelta(days=3))))
        if erase:
            _erase(holder)
        event = _stripe_event(
            "charge.refunded",
            {
                "id": "ch_1",
                "payment_intent": payment.stripe_payment_intent_id,
                "billing_details": {"name": "Ada Lovelace", "email": HOLDER_EMAIL},
                "refunds": {"data": [{"id": "re_1", "amount": 1000, "status": "succeeded", "metadata": {}}]},
            },
        )

        StripeEventHandler(event).handle_charge_refunded(event)

        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.REFUNDED  # the refund itself is recorded as before
        if erase:
            assert payment.raw_response == {}
        else:
            assert payment.raw_response["data"]["object"]["billing_details"]["email"] == HOLDER_EMAIL

    def test_a_cancelled_payment_intent_event_stores_no_payload_for_an_erased_buyer(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        payment = make_payment(make_ticket(holder, ended_event_factory(-timedelta(days=3))))
        _erase(holder)
        Payment.objects.filter(pk=payment.pk).update(status=Payment.PaymentStatus.PENDING)
        event = _stripe_event(
            "payment_intent.canceled", {"id": payment.stripe_payment_intent_id, "email": HOLDER_EMAIL}
        )

        StripeEventHandler(event).handle_payment_intent_canceled(event)

        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.FAILED
        assert payment.raw_response == {}

    @pytest.mark.parametrize("ticket_status", [Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.CANCELLED])
    def test_a_late_checkout_completion_stores_no_payload_for_an_erased_buyer(
        self, holder: RevelUser, ended_event_factory: Factory, ticket_status: str
    ) -> None:
        """Both writers of the completion: the normal one and the one for a cancelled ticket."""
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=3)))
        payment = make_payment(ticket)
        _erase(holder)
        Payment.objects.filter(pk=payment.pk).update(status=Payment.PaymentStatus.PENDING)
        Ticket.objects.filter(pk=ticket.pk).update(status=ticket_status)
        payment.refresh_from_db()
        ticket.refresh_from_db()
        payload = {"customer_details": {"email": HOLDER_EMAIL}}

        late = StripeEventHandler(_stripe_event("checkout.session.completed", {}))._apply_session_payments(
            [payment], {ticket.pk: ticket}, "pi_late", payload, "cs_late"
        )

        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED
        assert payment.raw_response == {}
        assert bool(late) == (ticket_status == Ticket.TicketStatus.CANCELLED)


def _stripe_event(event_type: str, obj: dict[str, t.Any]) -> t.Any:
    """A Stripe event double: ``dict(event)`` gives the payload, as for a real ``stripe.Event``."""
    payload = {"id": "evt_1", "type": event_type, "account": None, "livemode": False, "data": {"object": obj}}
    event = MagicMock(spec=stripe.Event)
    event.id, event.type, event.account, event.livemode = "evt_1", event_type, None, False
    event.data = MagicMock()
    event.data.object = obj
    event.keys.return_value = list(payload.keys())
    event.__getitem__.side_effect = payload.__getitem__
    return event


class TestSafety:
    def test_a_second_run_changes_nothing(self, holder: RevelUser, ended_event_factory: Factory) -> None:
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=1)))
        first = _erase(holder)

        second = _erase(first)

        ticket.refresh_from_db()
        assert ticket.holder_email == HOLDER_EMAIL  # not overwritten with the placeholder
        assert second.erased_at == first.erased_at

    def test_a_run_that_overlaps_an_erasure_changes_nothing(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        """Another run erases the account after this run read it and before this run takes the row lock."""
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=1)))

        def erase_meanwhile(user: RevelUser) -> None:
            fresh = RevelUser.objects.get(pk=user.pk)
            account_erasure.erase_account_keeping_holdings(fresh, kept_holdings(fresh, timezone.now()))

        with patch("accounts.tasks.gdpr.cleanup_referral_data", side_effect=erase_meanwhile):
            delete_user_account(str(holder.pk))

        ticket.refresh_from_db()
        assert ticket.holder_email == HOLDER_EMAIL  # not overwritten with the placeholder

    def test_force_full_delete_deletes_the_kept_holdings(self, holder: RevelUser, ended_event_factory: Factory) -> None:
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=1)))
        _erase(holder)

        delete_user_account(str(holder.pk), force_full_delete=True)

        assert not RevelUser.objects.filter(pk=holder.pk).exists()
        assert not Ticket.objects.filter(pk=ticket.pk).exists()

    def test_an_organization_owner_is_refused_and_nothing_changes(
        self, organization_owner_user: RevelUser, organization: Organization, ended_event_factory: Factory
    ) -> None:
        ticket = make_ticket(organization_owner_user, ended_event_factory(-timedelta(days=1)))

        with pytest.raises(ProtectedError):
            delete_user_account(str(organization_owner_user.pk))

        organization_owner_user.refresh_from_db()
        assert organization_owner_user.erased_at is None
        ticket.refresh_from_db()
        assert ticket.holder_email == ""

    def test_a_cascade_that_reaches_a_kept_ticket_rolls_back(
        self, holder: RevelUser, ended_event_factory: Factory
    ) -> None:
        """A future relation whose cascade removes a kept ticket must not slip through."""
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=1)))

        with patch.object(account_erasure, "_kept_rows", return_value=None), pytest.raises(RuntimeError):
            delete_user_account(str(holder.pk))

        holder.refresh_from_db()
        assert holder.erased_at is None
        assert holder.email == HOLDER_EMAIL
        assert Ticket.objects.filter(pk=ticket.pk).exists()

    def test_a_new_account_with_the_old_email_is_a_fresh_account(
        self, holder: RevelUser, ended_event_factory: Factory, revel_user_factory: RevelUserFactory
    ) -> None:
        ticket = make_ticket(holder, ended_event_factory(-timedelta(days=1)))
        _erase(holder)

        fresh = revel_user_factory.create_user(username=HOLDER_EMAIL, email=HOLDER_EMAIL)

        assert fresh.pk != holder.pk
        assert not Ticket.objects.filter(user=fresh).exists()
        assert Ticket.objects.get(pk=ticket.pk).user_id == holder.pk


def test_the_erasure_locks_the_account_row_before_anything_else(
    holder: RevelUser, ended_event_factory: Factory
) -> None:
    """An admin change post locks the row too (``RevelUserChangeForm``): it commits before the erasure or is refused.

    Without the early lock, a post could commit new inline rows after the erasure detached the old ones.
    """
    make_ticket(holder, ended_event_factory(-timedelta(days=2)))
    with CaptureQueriesContext(connection) as queries:
        delete_user_account(str(holder.pk))

    sqls = [query["sql"] for query in queries.captured_queries]
    lock = next(i for i, sql in enumerate(sqls) if "FOR NO KEY UPDATE" in sql and '"accounts_reveluser"' in sql)
    payment_lock = next(
        i for i, sql in enumerate(sqls) if "FOR UPDATE" in sql and sql.startswith('SELECT "events_payment"')
    )
    assert lock < payment_lock  # the first lock of release_capacity_of_account


class TestProfileFiles:
    """The profile files leave storage only when the erasure commits: a rollback cannot restore a file."""

    @pytest.fixture
    def holder_with_files(
        self, holder: RevelUser, ended_event_factory: Factory, settings: t.Any, tmp_path: t.Any
    ) -> RevelUser:
        settings.MEDIA_ROOT = str(tmp_path)
        make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        self._save_files(holder)
        return holder

    @staticmethod
    def _save_files(user: RevelUser) -> list[str]:
        storage = RevelUser._meta.get_field("profile_picture").storage
        names = {
            field: storage.save(f"protected/profile-pictures/{user.pk}/{field}.png", ContentFile(b"image"))
            for field in account_erasure._PROFILE_FILES
        }
        RevelUser.objects.filter(pk=user.pk).update(**names)
        return list(names.values())

    @staticmethod
    def _stored(user: RevelUser) -> list[bool]:
        stored = RevelUser.objects.values_list(*account_erasure._PROFILE_FILES).get(pk=user.pk)
        storage = RevelUser._meta.get_field("profile_picture").storage
        return [bool(name) and storage.exists(name) for name in stored]

    def test_a_failure_after_the_anonymisation_keeps_the_files(
        self, holder_with_files: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with (
            patch("accounts.service.gdpr.purge_user_history", side_effect=RuntimeError("x")),
            django_capture_on_commit_callbacks(execute=True),
            pytest.raises(RuntimeError),
        ):
            delete_user_account(str(holder_with_files.pk))

        assert self._stored(holder_with_files) == [True, True, True]

    def test_the_files_are_deleted_after_commit(
        self, holder_with_files: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        storage = RevelUser._meta.get_field("profile_picture").storage
        names = RevelUser.objects.values_list(*account_erasure._PROFILE_FILES).get(pk=holder_with_files.pk)
        with django_capture_on_commit_callbacks() as callbacks:
            delete_user_account(str(holder_with_files.pk))
        assert all(storage.exists(name) for name in names)  # not before the commit

        for callback in callbacks:
            callback()

        assert not any(storage.exists(name) for name in names)
        assert RevelUser.objects.get(pk=holder_with_files.pk).erased_at is not None

    def test_a_full_delete_deletes_the_picture_and_its_derived_images_after_commit(
        self, holder: RevelUser, settings: t.Any, tmp_path: t.Any, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        settings.MEDIA_ROOT = str(tmp_path)
        names = self._save_files(holder)
        storage = RevelUser._meta.get_field("profile_picture").storage
        with django_capture_on_commit_callbacks() as callbacks:
            delete_user_account(str(holder.pk))
        assert not RevelUser.objects.filter(pk=holder.pk).exists()
        assert all(storage.exists(name) for name in names)  # not before the commit

        for callback in callbacks:
            callback()

        assert not any(storage.exists(name) for name in names)

    def test_a_failed_full_delete_keeps_the_files(
        self, holder: RevelUser, settings: t.Any, tmp_path: t.Any, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        settings.MEDIA_ROOT = str(tmp_path)
        names = self._save_files(holder)
        storage = RevelUser._meta.get_field("profile_picture").storage
        with (
            patch("accounts.service.gdpr.purge_user_history", side_effect=RuntimeError("x")),
            django_capture_on_commit_callbacks(execute=True),
            pytest.raises(RuntimeError),
        ):
            delete_user_account(str(holder.pk))

        assert RevelUser.objects.filter(pk=holder.pk).exists()
        assert all(storage.exists(name) for name in names)

    @pytest.mark.parametrize("kept_ticket", [True, False], ids=["erasure", "full-delete"])
    def test_the_data_export_file_is_deleted_after_commit(
        self,
        holder: RevelUser,
        ended_event_factory: Factory,
        settings: t.Any,
        tmp_path: t.Any,
        django_capture_on_commit_callbacks: t.Any,
        kept_ticket: bool,
    ) -> None:
        """The export row cascades on both paths; the ZIP (its emailed link stays valid) must go too."""
        settings.MEDIA_ROOT = str(tmp_path)
        if kept_ticket:
            make_ticket(holder, ended_event_factory(-timedelta(days=2)))
        export = UserDataExport.objects.create(user=holder, status=UserDataExport.UserDataExportStatus.READY)
        export.file.save("export.zip", ContentFile(b"zip"), save=True)
        storage, name = export.file.storage, str(export.file.name)

        with django_capture_on_commit_callbacks() as callbacks:
            delete_user_account(str(holder.pk))
        assert storage.exists(name)  # not before the commit: a rollback cannot restore a file

        for callback in callbacks:
            callback()

        assert not storage.exists(name)
        assert not UserDataExport.objects.filter(pk=export.pk).exists()

    def test_a_failed_file_delete_is_logged_with_ids_and_the_others_are_deleted(
        self, holder_with_files: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        storage = RevelUser._meta.get_field("profile_picture").storage
        names = RevelUser.objects.values_list(*account_erasure._PROFILE_FILES).get(pk=holder_with_files.pk)
        real_delete = FileSystemStorage.delete  # the test storage backend

        def fail_first(self: t.Any, name: str) -> None:
            if name == names[0]:
                raise OSError("storage down")
            real_delete(self, name)

        with (
            patch.object(FileSystemStorage, "delete", fail_first),
            patch("accounts.service.account_erasure.logger") as log,
            patch("accounts.tasks.gdpr.retry_account_file_delete") as retry,
            django_capture_on_commit_callbacks(execute=True),
        ):
            delete_user_account(str(holder_with_files.pk))

        log.error.assert_called_once_with(
            "account_erasure_profile_file_delete_failed", user_id=str(holder_with_files.pk), field="profile_picture"
        )
        assert [storage.exists(name) for name in names] == [True, False, False]
        retry.delay.assert_called_once_with(user_id=str(holder_with_files.pk), files=[("profile_picture", names[0])])

    def test_the_retry_deletes_the_files_and_raises_on_a_storage_failure(self, holder_with_files: RevelUser) -> None:
        """A raise lets Celery try again with backoff (``autoretry_for``)."""
        from accounts.tasks.gdpr import retry_account_file_delete

        storage = RevelUser._meta.get_field("profile_picture").storage
        names = RevelUser.objects.values_list(*account_erasure._PROFILE_FILES).get(pk=holder_with_files.pk)
        files = [("profile_picture", names[0]), ("profile_picture_preview", names[2])]
        with patch.object(FileSystemStorage, "delete", side_effect=OSError("storage down")), pytest.raises(OSError):
            retry_account_file_delete.run(user_id=str(holder_with_files.pk), files=files)
        assert storage.exists(names[0])

        retry_account_file_delete.run(user_id=str(holder_with_files.pk), files=files)

        assert [storage.exists(name) for name in names] == [False, True, False]

    def test_a_picture_saved_after_the_first_lookup_is_deleted_too(
        self, holder_with_files: RevelUser, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        """An admin saves a new picture while the task runs its steps before the transaction."""
        storage = RevelUser._meta.get_field("profile_picture").storage
        new_name = storage.save(f"protected/profile-pictures/{holder_with_files.pk}/new.png", ContentFile(b"new"))

        def save_new_picture(user: RevelUser) -> None:
            RevelUser.objects.filter(pk=user.pk).update(profile_picture=new_name)

        with (
            patch("accounts.tasks.gdpr.cleanup_referral_data", side_effect=save_new_picture),
            django_capture_on_commit_callbacks(execute=True),
        ):
            delete_user_account(str(holder_with_files.pk))

        assert not storage.exists(new_name)
