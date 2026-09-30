"""Tests for the Brevo email-events webhook endpoint (#1032)."""

import base64
import json
import typing as t
import uuid

import pytest
from django.test import override_settings
from django.test.client import Client

from events.models import Organization
from notifications.enums import DeliveryChannel, DeliveryStatus
from notifications.models import EmailSuppression, Notification, NotificationDelivery

pytestmark = pytest.mark.django_db

URL = "/api/email-events/brevo"
SECRET = "s3cret-webhook-value"


def _basic(password: str, user: str = "brevo") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _post(client: Client, body: t.Any, authorization: str | None = None, raw: bytes | None = None) -> t.Any:
    headers = {"Authorization": authorization} if authorization is not None else {}
    data = raw if raw is not None else json.dumps(body)
    return client.post(URL, data=data, content_type="application/json", headers=headers)


HARD_BOUNCE = {"event": "hard_bounce", "email": "bounced@example.com", "reason": "550 no such user"}


class TestAuth:
    @override_settings(EMAIL_WEBHOOK_SECRET="")
    def test_unconfigured_is_404_and_inert(self, client: Client) -> None:
        response = _post(client, HARD_BOUNCE, authorization=f"Bearer {SECRET}")
        assert response.status_code == 404
        assert not EmailSuppression.objects.exists()

    @override_settings(EMAIL_WEBHOOK_SECRET=SECRET)
    @pytest.mark.parametrize(
        "authorization",
        [
            None,
            "",
            "Bearer wrong",
            f"Bearer {SECRET}x",
            f"bearer{SECRET}",
            _basic("wrong"),
            "Basic !!!not-base64!!!",
            "Basic " + base64.b64encode(SECRET.encode()).decode(),  # no colon
            f"Token {SECRET}",
            SECRET,
        ],
    )
    def test_wrong_or_missing_secret_is_401_no_state_change(self, client: Client, authorization: str | None) -> None:
        response = _post(client, HARD_BOUNCE, authorization=authorization)
        assert response.status_code == 401
        assert not EmailSuppression.objects.exists()

    @override_settings(EMAIL_WEBHOOK_SECRET=SECRET)
    def test_bad_credentials_checked_before_body(self, client: Client) -> None:
        response = _post(client, None, authorization="Bearer wrong", raw=b"not json")
        assert response.status_code == 401

    @override_settings(EMAIL_WEBHOOK_SECRET=SECRET)
    @pytest.mark.parametrize("authorization", [f"Bearer {SECRET}", _basic(SECRET), _basic(SECRET, user="")])
    def test_valid_credentials_accepted(self, client: Client, authorization: str) -> None:
        response = _post(client, HARD_BOUNCE, authorization=authorization)
        assert response.status_code == 200
        assert EmailSuppression.objects.get().reason == EmailSuppression.Reason.HARD_BOUNCE


class TestEvents:
    AUTH = f"Bearer {SECRET}"

    @pytest.fixture(autouse=True)
    def _secret(self, settings: t.Any) -> None:
        settings.EMAIL_WEBHOOK_SECRET = SECRET

    @pytest.fixture
    def sent_delivery(self, notification: Notification) -> NotificationDelivery:
        return NotificationDelivery.objects.create(
            notification=notification, channel=DeliveryChannel.EMAIL, status=DeliveryStatus.SENT
        )

    @pytest.mark.parametrize(
        ("event_name", "reason"),
        [
            ("hard_bounce", EmailSuppression.Reason.HARD_BOUNCE),
            ("hardBounce", EmailSuppression.Reason.HARD_BOUNCE),
            ("spam", EmailSuppression.Reason.COMPLAINT),
            ("blocked", EmailSuppression.Reason.BLOCKED),
            ("invalid_email", EmailSuppression.Reason.INVALID),
        ],
    )
    def test_each_suppressing_kind(self, client: Client, event_name: str, reason: EmailSuppression.Reason) -> None:
        response = _post(client, {"event": event_name, "email": "x@example.com"}, self.AUTH)
        assert response.status_code == 200
        assert EmailSuppression.objects.get(email="x@example.com").reason == reason

    @pytest.mark.parametrize("event_name", ["soft_bounce", "deferred", "unsubscribed", "delivered", "brand_new"])
    def test_ignored_kinds_are_200_without_state(self, client: Client, event_name: str) -> None:
        response = _post(client, {"event": event_name, "email": "x@example.com"}, self.AUTH)
        assert response.status_code == 200
        assert not EmailSuppression.objects.exists()

    def test_replay_is_idempotent(self, client: Client, sent_delivery: NotificationDelivery) -> None:
        body = {**HARD_BOUNCE, "X-Mailin-custom": f"delivery:{sent_delivery.id}"}
        assert _post(client, body, self.AUTH).status_code == 200
        assert _post(client, body, self.AUTH).status_code == 200
        assert EmailSuppression.objects.count() == 1
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.FAILED

    def test_correlated_bounce_fails_delivery(
        self, client: Client, sent_delivery: NotificationDelivery, organization: Organization
    ) -> None:
        body = {**HARD_BOUNCE, "X-Mailin-custom": f"delivery:{sent_delivery.id}|org:{organization.id}"}
        assert _post(client, body, self.AUTH).status_code == 200
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.FAILED
        assert sent_delivery.metadata["suppression_reason"] == EmailSuppression.Reason.HARD_BOUNCE
        assert EmailSuppression.objects.get().organization_id == organization.id

    def test_complaint_keeps_delivery_sent_and_attributes_org(
        self, client: Client, sent_delivery: NotificationDelivery, organization: Organization
    ) -> None:
        body = {
            "event": "spam",
            "email": "complainer@example.com",
            "X-Mailin-custom": f"delivery:{sent_delivery.id}|org:{organization.id}",
        }
        assert _post(client, body, self.AUTH).status_code == 200
        sent_delivery.refresh_from_db()
        assert sent_delivery.status == DeliveryStatus.SENT
        assert sent_delivery.metadata["complained"] is True
        assert (
            EmailSuppression.objects.filter(reason=EmailSuppression.Reason.COMPLAINT, organization=organization).count()
            == 1
        )

    def test_list_payload(self, client: Client) -> None:
        body = [
            {"event": "hard_bounce", "email": "a@example.com"},
            {"event": "spam", "email": "b@example.com"},
            {"event": "delivered", "email": "c@example.com"},
        ]
        assert _post(client, body, self.AUTH).status_code == 200
        assert set(EmailSuppression.objects.values_list("email", flat=True)) == {"a@example.com", "b@example.com"}

    def test_garbled_custom_header(self, client: Client) -> None:
        body = {**HARD_BOUNCE, "X-Mailin-custom": "delivery:%%%|org:|||"}
        assert _post(client, body, self.AUTH).status_code == 200
        assert EmailSuppression.objects.get().organization_id is None

    def test_invitation_correlation_attributes_org(self, client: Client, organization: Organization) -> None:
        body = {**HARD_BOUNCE, "X-Mailin-custom": f"invitation:{uuid.uuid4()}|org:{organization.id}"}
        assert _post(client, body, self.AUTH).status_code == 200
        assert EmailSuppression.objects.get().organization_id == organization.id

    def test_malformed_json_is_400(self, client: Client) -> None:
        response = _post(client, None, self.AUTH, raw=b"{not json")
        assert response.status_code == 400
        assert not EmailSuppression.objects.exists()
