"""Tests for provider email events (#1032): Brevo payload adapter and suppression recording."""

import typing as t
import uuid

import pytest
from django.core.exceptions import ValidationError

from events.models import Organization
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import EmailSuppression, Notification, NotificationDelivery
from notifications.service.email_events import (
    ProviderEmailEvent,
    load_json_body,
    parse_brevo,
    record_email_event,
)

pytestmark = pytest.mark.django_db

Reason = EmailSuppression.Reason


def _event(
    kind: t.Literal["hard_bounce", "complaint", "blocked", "invalid", "ignored"],
    *,
    email: str = "bounced@example.com",
    delivery_id: uuid.UUID | None = None,
    organization_id: uuid.UUID | None = None,
    detail: str = "550 mailbox unavailable",
) -> ProviderEmailEvent:
    return ProviderEmailEvent(
        email=email,
        kind=kind,
        delivery_id=delivery_id,
        organization_id=organization_id,
        invitation_id=None,
        detail=detail,
    )


@pytest.fixture
def sent_delivery(notification: Notification) -> NotificationDelivery:
    return NotificationDelivery.objects.create(
        notification=notification,
        channel=DeliveryChannel.EMAIL,
        status=DeliveryStatus.SENT,
        metadata={"email_log_id": "abc"},
    )


class TestParseBrevo:
    @pytest.mark.parametrize(
        ("event_name", "kind"),
        [
            ("hard_bounce", "hard_bounce"),
            ("hardBounce", "hard_bounce"),
            ("spam", "complaint"),
            ("blocked", "blocked"),
            ("invalid_email", "invalid"),
            ("invalid", "invalid"),
            ("soft_bounce", "ignored"),
            ("softBounce", "ignored"),
            ("deferred", "ignored"),
            ("error", "ignored"),
            ("unsubscribed", "ignored"),
            ("delivered", "ignored"),
            ("request", "ignored"),
            ("opened", "ignored"),
            ("something_new", "ignored"),
        ],
    )
    def test_event_kinds(self, event_name: str, kind: str) -> None:
        [ev] = parse_brevo({"event": event_name, "email": "a@example.com", "reason": "why"})
        assert ev.kind == kind
        assert ev.email == "a@example.com"
        assert ev.detail == "why"

    def test_notification_correlation_header(self) -> None:
        delivery_id, org_id = uuid.uuid4(), uuid.uuid4()
        [ev] = parse_brevo(
            {"event": "spam", "email": "a@example.com", "X-Mailin-custom": f"delivery:{delivery_id}|org:{org_id}"}
        )
        assert (ev.delivery_id, ev.organization_id, ev.invitation_id) == (delivery_id, org_id, None)

    def test_invitation_correlation_header(self) -> None:
        invitation_id, org_id = uuid.uuid4(), uuid.uuid4()
        [ev] = parse_brevo(
            {
                "event": "hard_bounce",
                "email": "a@example.com",
                "X-Mailin-custom": f"invitation:{invitation_id}|org:{org_id}",
            }
        )
        assert (ev.delivery_id, ev.organization_id, ev.invitation_id) == (None, org_id, invitation_id)

    @pytest.mark.parametrize(
        "custom",
        [
            "",
            "garbage",
            "delivery:not-a-uuid|org:",
            "|||:::",
            "delivery|org",
            None,
            123,
            {"delivery": "x"},
        ],
    )
    def test_garbled_custom_header_is_tolerated(self, custom: t.Any) -> None:
        [ev] = parse_brevo({"event": "hard_bounce", "email": "a@example.com", "X-Mailin-custom": custom})
        assert ev.kind == "hard_bounce"
        assert (ev.delivery_id, ev.organization_id, ev.invitation_id) == (None, None, None)

    def test_partially_garbled_header_keeps_valid_parts(self) -> None:
        org_id = uuid.uuid4()
        [ev] = parse_brevo(
            {"event": "hard_bounce", "email": "a@example.com", "X-Mailin-custom": f"delivery:nope|org:{org_id}|x"}
        )
        assert (ev.delivery_id, ev.organization_id) == (None, org_id)

    def test_list_payload(self) -> None:
        events = parse_brevo(
            [
                {"event": "hard_bounce", "email": "a@example.com"},
                {"event": "spam", "email": "b@example.com"},
            ]
        )
        assert [(e.kind, e.email) for e in events] == [("hard_bounce", "a@example.com"), ("complaint", "b@example.com")]

    def test_items_without_email_or_non_dicts_are_skipped(self) -> None:
        events = parse_brevo([{"event": "hard_bounce"}, "junk", 42, {"event": "spam", "email": "b@example.com"}])
        assert [e.email for e in events] == ["b@example.com"]

    def test_missing_reason_is_empty_detail(self) -> None:
        [ev] = parse_brevo({"event": "blocked", "email": "a@example.com"})
        assert ev.detail == ""


class TestLoadJsonBody:
    @pytest.mark.parametrize("body", [b"", b"not json", b'"a string"', b"42", b"\xff\xfe"])
    def test_rejects_non_object_bodies(self, body: bytes) -> None:
        with pytest.raises(ValidationError):
            load_json_body(body)

    def test_accepts_dict_and_list(self) -> None:
        assert load_json_body(b'{"event": "spam"}') == {"event": "spam"}
        assert load_json_body(b"[]") == []


class TestRecordEmailEvent:
    @pytest.mark.parametrize(
        ("kind", "reason"),
        [("hard_bounce", Reason.HARD_BOUNCE), ("invalid", Reason.INVALID), ("blocked", Reason.BLOCKED)],
    )
    def test_bounce_like_suppresses_and_fails_delivery(
        self,
        kind: t.Literal["hard_bounce", "invalid", "blocked"],
        reason: EmailSuppression.Reason,
        sent_delivery: NotificationDelivery,
        organization: Organization,
    ) -> None:
        record_email_event(
            _event(kind, email="Bounced@Example.com", delivery_id=sent_delivery.id, organization_id=organization.id)
        )

        row = EmailSuppression.objects.get()
        assert (row.email, row.reason, row.source) == ("bounced@example.com", reason, EmailSuppression.Source.PROVIDER)
        assert row.organization_id == organization.id
        assert row.detail == "550 mailbox unavailable"
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.FAILED
        assert sent_delivery.metadata == {"email_log_id": "abc", "suppression_reason": reason}
        assert reason in sent_delivery.error_message

    def test_complaint_suppresses_and_keeps_delivery_sent(
        self, sent_delivery: NotificationDelivery, organization: Organization
    ) -> None:
        record_email_event(_event("complaint", delivery_id=sent_delivery.id, organization_id=organization.id))

        row = EmailSuppression.objects.get()
        assert (row.reason, row.organization_id) == (Reason.COMPLAINT, organization.id)
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.SENT
        assert sent_delivery.metadata == {"email_log_id": "abc", "complained": True}

    def test_ignored_is_noop(self, sent_delivery: NotificationDelivery) -> None:
        record_email_event(_event("ignored", delivery_id=sent_delivery.id))
        assert not EmailSuppression.objects.exists()
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.SENT

    def test_replay_is_idempotent(self, sent_delivery: NotificationDelivery) -> None:
        ev = _event("hard_bounce", delivery_id=sent_delivery.id)
        record_email_event(ev)
        sent_delivery.refresh_from_db()
        first = (sent_delivery.status, sent_delivery.metadata, sent_delivery.error_message)

        record_email_event(ev)

        assert EmailSuppression.objects.count() == 1
        sent_delivery.refresh_from_db()
        assert (sent_delivery.status, sent_delivery.metadata, sent_delivery.error_message) == first

    def test_unknown_delivery_and_org_still_suppress(self) -> None:
        record_email_event(_event("hard_bounce", delivery_id=uuid.uuid4(), organization_id=uuid.uuid4()))
        row = EmailSuppression.objects.get()
        # A dangling org id must not violate the FK: attribution is dropped instead.
        assert row.organization_id is None

    def test_non_email_delivery_is_not_touched(self, notification: Notification) -> None:
        in_app = NotificationDelivery.objects.create(
            notification=notification, channel=DeliveryChannel.IN_APP, status=DeliveryStatus.SENT
        )
        record_email_event(_event("hard_bounce", delivery_id=in_app.id))
        in_app.refresh_from_db()
        assert in_app.status == DeliveryStatus.SENT
        assert in_app.metadata == {}

    def test_complaint_upgrades_prior_bounce(self, organization: Organization) -> None:
        record_email_event(_event("hard_bounce"))
        record_email_event(_event("complaint", organization_id=organization.id))
        row = EmailSuppression.objects.get()
        assert (row.reason, row.organization_id) == (Reason.COMPLAINT, organization.id)

    def test_malformed_address_skips_suppression_but_fails_delivery(self, sent_delivery: NotificationDelivery) -> None:
        # A row that can't pass full_clean must not 400 the webhook (Brevo would retry forever).
        record_email_event(_event("invalid", email="not an address", delivery_id=sent_delivery.id))
        assert not EmailSuppression.objects.exists()
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.FAILED
