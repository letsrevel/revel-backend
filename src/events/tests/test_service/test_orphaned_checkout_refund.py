"""A completed ticket or series-pass checkout with no Payment rows is refunded in full at Stripe (#1489)."""

import typing as t
import uuid
from unittest.mock import MagicMock, patch

import pytest
import stripe
from celery.exceptions import Retry
from structlog.testing import capture_logs

from events.models import Event, Organization, Payment, Ticket, TicketTier
from events.service.stripe_webhooks import StripeEventHandler, handle_event
from events.tasks.refunds import ORPHANED_CHECKOUT_REFUND_MAX_RETRIES, refund_orphaned_checkout_session

pytestmark = pytest.mark.django_db

_ACCOUNT = "acct_orphan_1489"


@pytest.fixture
def connected_org(organization: Organization) -> Organization:
    organization.stripe_account_id = _ACCOUNT
    organization.save(update_fields=["stripe_account_id"])
    return organization


@pytest.fixture
def stripe_refund() -> t.Iterator[MagicMock]:
    with patch("stripe.Refund.create") as mock_create:
        mock_create.return_value.id = "re_orphan"
        yield mock_create


def _session_event(
    *,
    session_id: str = "cs_orphan",
    metadata: dict[str, str] | None = None,
    mode: str = "payment",
    account: str | None = _ACCOUNT,
    event_id: str = "evt_orphan",
) -> stripe.Event:
    session = {
        "object": "checkout.session",
        "id": session_id,
        "mode": mode,
        "payment_status": "paid",
        "amount_total": 5000,
        "currency": "eur",
        "payment_intent": f"pi_{session_id}",
        "metadata": metadata or {},
    }
    payload: dict[str, t.Any] = {
        "id": event_id,
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": session},
    }
    if account is not None:
        payload["account"] = account
    return stripe.Event.construct_from(payload, "sk_test")


def _ticket_metadata(event: Event) -> dict[str, str]:
    """The metadata a ticket checkout session gets (``_create_stripe_session``)."""
    return {"ticket_ids": str(uuid.uuid4()), "event_id": str(event.id), "user_id": str(uuid.uuid4()), "batch_size": "1"}


def _pass_metadata() -> dict[str, str]:
    """The metadata a series-pass checkout session gets (``_create_series_pass_stripe_session``)."""
    return {"held_pass_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()), "ticket_ids": str(uuid.uuid4())}


def _deliver(stripe_event: stripe.Event, capture: t.Any) -> list[t.MutableMapping[str, t.Any]]:
    """Run the webhook entry point and its on-commit callbacks (the refund task runs eagerly)."""
    with capture_logs() as logs:
        with capture(execute=True):
            handle_event(stripe_event)
    return logs


def _lines(logs: list[t.MutableMapping[str, t.Any]], name: str) -> list[t.MutableMapping[str, t.Any]]:
    return [line for line in logs if line["event"] == name]


class TestWebhookRefundsTheOrphanedSession:
    def test_a_ticket_session_is_refunded_in_full_and_logged_with_ids(
        self,
        connected_org: Organization,
        event: Event,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        logs = _deliver(_session_event(metadata=_ticket_metadata(event)), django_capture_on_commit_callbacks)

        stripe_refund.assert_called_once_with(
            payment_intent="pi_cs_orphan",
            metadata={"source": "orphaned_checkout"},
            idempotency_key="orphaned-checkout-refund:cs_orphan",
            stripe_account=_ACCOUNT,
        )
        # Full refund (no amount), and the platform fee is kept (no refund_application_fee).
        assert "amount" not in stripe_refund.call_args.kwargs
        assert "refund_application_fee" not in stripe_refund.call_args.kwargs
        (alert,) = _lines(logs, "stripe_session_paid_without_payments")
        assert alert["log_level"] == "error"
        assert alert["organization_id"] == str(connected_org.pk)
        assert alert["refund_queued"] is True
        (refunded,) = _lines(logs, "orphaned_checkout_refunded")
        assert refunded["log_level"] == "error"
        assert refunded == {
            "event": "orphaned_checkout_refunded",
            "log_level": "error",
            "refund_id": "re_orphan",
            "session_id": "cs_orphan",
            "payment_intent_id": "pi_cs_orphan",
            "organization_id": str(connected_org.pk),
        }

    def test_a_series_pass_session_is_refunded(
        self, connected_org: Organization, stripe_refund: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _deliver(_session_event(metadata=_pass_metadata()), django_capture_on_commit_callbacks)

        stripe_refund.assert_called_once()
        assert stripe_refund.call_args.kwargs["stripe_account"] == _ACCOUNT

    def test_a_platform_account_session_is_refunded_without_the_account_header(
        self,
        organization: Organization,
        event: Event,
        settings: t.Any,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        organization.stripe_account_id = settings.STRIPE_ACCOUNT
        organization.save(update_fields=["stripe_account_id"])

        _deliver(_session_event(metadata=_ticket_metadata(event), account=None), django_capture_on_commit_callbacks)

        stripe_refund.assert_called_once()
        assert "stripe_account" not in stripe_refund.call_args.kwargs

    def test_a_replayed_webhook_does_not_refund_again(
        self,
        connected_org: Organization,
        event: Event,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        stripe_event = _session_event(metadata=_ticket_metadata(event))
        _deliver(stripe_event, django_capture_on_commit_callbacks)
        logs = _deliver(stripe_event, django_capture_on_commit_callbacks)

        stripe_refund.assert_called_once()
        assert _lines(logs, "stripe_webhook_duplicate")
        assert not _lines(logs, "stripe_session_paid_without_payments")


class TestOnlyRevelTicketAndPassSessionsAreRefunded:
    def test_a_subscription_session_is_not_refunded(
        self, connected_org: Organization, stripe_refund: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        stripe_event = _session_event(
            mode="subscription", metadata={"membership_subscription_id": str(uuid.uuid4()), "ticket_ids": "x"}
        )
        logs = _deliver(stripe_event, django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()
        assert not _lines(logs, "stripe_session_paid_without_payments")

    def test_a_session_without_revel_checkout_metadata_keeps_the_alert_only(
        self, connected_org: Organization, stripe_refund: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        logs = _deliver(_session_event(metadata={"order": "external"}), django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()
        (alert,) = _lines(logs, "stripe_session_paid_without_payments")
        assert alert["refund_queued"] is False
        assert alert["organization_id"] is None

    def test_a_session_on_an_account_of_no_organization_is_not_refunded(
        self, event: Event, stripe_refund: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        _deliver(
            _session_event(metadata=_ticket_metadata(event), account="acct_unknown"), django_capture_on_commit_callbacks
        )

        stripe_refund.assert_not_called()

    def test_a_ticket_session_whose_event_is_of_another_organization_is_not_refunded(
        self,
        connected_org: Organization,
        event: Event,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        other = Organization.objects.create(name="Other", slug="other-1489", owner=connected_org.owner)
        Event.objects.filter(pk=event.pk).update(organization=other)

        _deliver(_session_event(metadata=_ticket_metadata(event)), django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()

    def test_a_malformed_event_id_is_not_refunded(
        self, connected_org: Organization, stripe_refund: MagicMock, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        metadata = {"ticket_ids": "x", "event_id": "not-a-uuid", "user_id": str(uuid.uuid4()), "batch_size": "1"}
        _deliver(_session_event(metadata=metadata), django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()

    @pytest.mark.parametrize(
        "metadata",
        [
            {"ticket_ids": "A1,A2"},  # another app on the organizer's Standard account
            {"ticket_ids": "x", "user_id": str(uuid.uuid4()), "held_pass_id": "not-a-uuid"},
            {"ticket_ids": "x", "held_pass_id": str(uuid.uuid4())},  # no user_id
        ],
    )
    def test_a_session_without_the_full_revel_metadata_is_not_refunded(
        self,
        connected_org: Organization,
        metadata: dict[str, str],
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        logs = _deliver(_session_event(metadata=metadata), django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()
        (alert,) = _lines(logs, "stripe_session_paid_without_payments")
        assert alert["refund_queued"] is False

    def test_a_ticket_session_of_a_deleted_event_is_refunded_on_its_connected_account(
        self,
        connected_org: Organization,
        event: Event,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        metadata = _ticket_metadata(event)
        event.delete()

        _deliver(_session_event(metadata=metadata), django_capture_on_commit_callbacks)

        stripe_refund.assert_called_once()

    def test_a_ticket_session_of_a_deleted_event_on_the_platform_account_is_not_refunded(
        self,
        organization: Organization,
        event: Event,
        settings: t.Any,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        organization.stripe_account_id = settings.STRIPE_ACCOUNT
        organization.save(update_fields=["stripe_account_id"])
        metadata = _ticket_metadata(event)
        event.delete()

        _deliver(_session_event(metadata=metadata, account=None), django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()

    def test_a_session_in_another_mode_is_not_refunded(
        self,
        connected_org: Organization,
        event: Event,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        stripe_event = _session_event(mode="setup", metadata=_ticket_metadata(event))
        logs = _deliver(stripe_event, django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()
        (alert,) = _lines(logs, "stripe_session_paid_without_payments")
        assert alert["refund_queued"] is False

    def test_a_session_without_a_payment_intent_is_not_refunded(
        self,
        connected_org: Organization,
        event: Event,
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        stripe_event = _session_event(metadata=_ticket_metadata(event))
        stripe_event.data.object["payment_intent"] = None
        logs = _deliver(stripe_event, django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()
        (alert,) = _lines(logs, "stripe_session_paid_without_payments")
        assert alert["refund_queued"] is False

    def test_a_session_with_payment_rows_is_not_refunded(
        self,
        connected_org: Organization,
        event: Event,
        ticket_factory: t.Callable[..., Ticket],
        payment_factory: t.Callable[..., Payment],
        tier_factory: t.Callable[..., TicketTier],
        stripe_refund: MagicMock,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        ticket = ticket_factory(tier=tier_factory(), status=Ticket.TicketStatus.PENDING)
        payment = payment_factory(
            ticket, status=Payment.PaymentStatus.PENDING, stripe_session_id="cs_orphan", stripe_payment_intent_id=None
        )
        stripe_event = _session_event(metadata=_ticket_metadata(event))
        stripe_event.data.object.pop("amount_total")  # no total to reconcile against

        with patch("events.tasks.generate_attendee_invoice_task.delay"):
            _deliver(stripe_event, django_capture_on_commit_callbacks)

        stripe_refund.assert_not_called()
        payment.refresh_from_db()
        assert payment.status == Payment.PaymentStatus.SUCCEEDED


class TestRefundTaskFailures:
    _ARGS = ("cs_task", "pi_task", _ACCOUNT, "org-id")

    def test_a_transient_error_is_retried(self, stripe_refund: MagicMock) -> None:
        stripe_refund.side_effect = [stripe.error.APIConnectionError("network")] * 2 + [MagicMock(id="re_retry")]

        with capture_logs() as logs:
            with pytest.raises(Retry) as retry:
                refund_orphaned_checkout_session.apply(args=self._ARGS)
            assert retry.value.when == 60
            with pytest.raises(Retry) as later_retry:
                refund_orphaned_checkout_session.apply(args=self._ARGS, retries=2)
            assert later_retry.value.when == 240  # exponential backoff
            # The retry Celery schedules: the same task with retries=1.
            refund_orphaned_checkout_session.apply(args=self._ARGS, retries=1)

        assert stripe_refund.call_count == 3
        keys = {call.kwargs["idempotency_key"] for call in stripe_refund.call_args_list}
        assert keys == {"orphaned-checkout-refund:cs_task"}  # the retry cannot refund twice
        assert _lines(logs, "orphaned_checkout_refund_retry")
        assert not _lines(logs, "orphaned_checkout_refund_failed")
        (refunded,) = _lines(logs, "orphaned_checkout_refunded")
        assert refunded["refund_id"] == "re_retry"

    def test_a_transient_error_after_the_last_retry_is_logged(self, stripe_refund: MagicMock) -> None:
        stripe_refund.side_effect = stripe.error.RateLimitError("slow down")

        with capture_logs() as logs:
            refund_orphaned_checkout_session.apply(args=self._ARGS, retries=ORPHANED_CHECKOUT_REFUND_MAX_RETRIES)

        stripe_refund.assert_called_once()
        (failed,) = _lines(logs, "orphaned_checkout_refund_failed")
        assert failed["log_level"] == "error"
        assert (failed["session_id"], failed["payment_intent_id"]) == ("cs_task", "pi_task")
        assert not _lines(logs, "orphaned_checkout_refund_retry")
        assert not _lines(logs, "orphaned_checkout_refunded")

    def test_a_terminal_error_is_logged_and_not_retried(self, stripe_refund: MagicMock) -> None:
        stripe_refund.side_effect = stripe.error.InvalidRequestError(
            "no such intent", param=None, code="resource_missing"
        )

        with capture_logs() as logs:
            refund_orphaned_checkout_session.apply(args=self._ARGS)

        stripe_refund.assert_called_once()
        (failed,) = _lines(logs, "orphaned_checkout_refund_failed")
        assert failed["log_level"] == "error"
        assert failed["code"] == "resource_missing"

    def test_an_already_refunded_charge_is_a_no_op(self, stripe_refund: MagicMock) -> None:
        stripe_refund.side_effect = stripe.error.InvalidRequestError(
            "already refunded", param=None, code="charge_already_refunded"
        )

        with capture_logs() as logs:
            refund_orphaned_checkout_session.apply(args=self._ARGS)

        assert _lines(logs, "orphaned_checkout_refund_already_refunded")
        assert not _lines(logs, "orphaned_checkout_refund_failed")
        assert not _lines(logs, "orphaned_checkout_refunded")

    def test_a_failed_dispatch_is_logged_with_the_ids(
        self, connected_org: Organization, event: Event, django_capture_on_commit_callbacks: t.Any
    ) -> None:
        with patch("events.tasks.refunds.refund_orphaned_checkout_session.delay", side_effect=RuntimeError("broker")):
            logs = _deliver(_session_event(metadata=_ticket_metadata(event)), django_capture_on_commit_callbacks)

        (failed,) = _lines(logs, "orphaned_checkout_refund_dispatch_failed")
        assert failed["log_level"] == "error"
        assert failed["session_id"] == "cs_orphan"
        assert failed["organization_id"] == str(connected_org.pk)


def test_the_handler_does_not_refund_from_the_replay_path(
    connected_org: Organization, event: Event, stripe_refund: MagicMock, django_capture_on_commit_callbacks: t.Any
) -> None:
    """A duplicate delivery can arrive after a confirmed sale's rows were deleted with the account."""
    stripe_event = _session_event(metadata=_ticket_metadata(event))
    with django_capture_on_commit_callbacks(execute=True):
        StripeEventHandler(stripe_event).replay()

    stripe_refund.assert_not_called()
