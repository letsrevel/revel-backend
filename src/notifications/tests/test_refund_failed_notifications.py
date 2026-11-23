"""Templates and routing of the refund-failed notifications (#1507)."""

import typing as t

import pytest
from django.utils import translation

from accounts.models import RevelUser
from events.models import Organization, OrganizationStaff, PermissionMap, PermissionsSchema
from notifications.context_schemas import NOTIFICATION_CONTEXT_SCHEMAS
from notifications.enums import MANDATORY_TYPES, TRANSACTIONAL_TYPES, NotificationType
from notifications.service.eligibility import get_staff_for_notification
from notifications.service.templates.registry import get_template
from notifications.tasks import dispatch_notification
from notifications.tests.conftest import _create_notification_for_test

pytestmark = pytest.mark.django_db

_STAFF_CONTEXT: dict[str, t.Any] = {
    "organization_id": "00000000-0000-0000-0000-000000000001",
    "organization_name": "Chess Club",
    "kind": "ticket",
    "refund_id": "re_1",
    "payment_intent_id": "pi_1",
    "refund_amount": "40.00",
    "currency": "EUR",
    "failure_reason": "expired_or_canceled_card",
    "holder_name": "Ada",
    "holder_email": "ada@example.com",
    "event_id": "00000000-0000-0000-0000-000000000002",
    "event_name": "Summer Fest",
    "credit_note_numbers": ["CN-1", "CN-2"],
    "resolve_url": "https://example.test/org/chess/admin/events/e/tickets?search=ada",
}


def _user(factory: t.Any, name: str) -> RevelUser:
    """A user whose address passes the one-click-unsubscribe email validation."""
    return t.cast(RevelUser, factory(username=f"{name}@example.com", email=f"{name}@example.com"))


def _channels(notification: t.Any, notification_type: NotificationType) -> list[str]:
    template = get_template(notification_type)
    html = template.get_email_html_body(notification)
    assert html
    return [
        template.get_in_app_title(notification),
        template.get_email_subject(notification),
        template.get_in_app_body(notification),
        template.get_email_text_body(notification),
        html,
        template.get_telegram_body(notification),
    ]


def test_both_types_have_a_context_schema_and_are_delivered_at_once() -> None:
    for notification_type in (NotificationType.REFUND_FAILED, NotificationType.TICKET_REFUND_FAILED):
        assert notification_type in NOTIFICATION_CONTEXT_SCHEMAS
        assert notification_type in TRANSACTIONAL_TYPES
    # The buyer's money is at stake, so the buyer cannot opt out; staff alerts stay opt-out-able.
    assert NotificationType.TICKET_REFUND_FAILED in MANDATORY_TYPES
    assert NotificationType.REFUND_FAILED not in MANDATORY_TYPES


def test_staff_ticket_message_names_the_refund_the_credit_notes_and_the_link(revel_user_factory: t.Any) -> None:
    user = _user(revel_user_factory, "u1")
    notification = _create_notification_for_test(
        user=user, notification_type=NotificationType.REFUND_FAILED, context=_STAFF_CONTEXT
    )

    rendered = _channels(notification, NotificationType.REFUND_FAILED)

    for text in rendered[2:]:
        assert "40.00 EUR" in text
        assert "Summer Fest" in text
        assert "expired_or_canceled_card" in text
        assert "CN-1, CN-2" in text
        assert "subscription" not in text
        # The failed refund is described alone: another refund can have left the payment REFUNDED.
        assert "did not get this refund" in text
        assert "not refunded" not in text
    assert rendered[0] == "Refund failed"
    assert "Chess Club" in rendered[1]
    for text in (rendered[3], rendered[4], rendered[5]):
        assert _STAFF_CONTEXT["resolve_url"] in text


def test_staff_membership_message_says_the_subscription_stays_cancelled(revel_user_factory: t.Any) -> None:
    context = {k: v for k, v in _STAFF_CONTEXT.items() if k not in ("event_id", "event_name", "resolve_url")}
    context.update(
        kind="membership", plan_name="Monthly", credit_note_numbers=[], subscription_cancelled_by_refund=True
    )
    notification = _create_notification_for_test(
        user=_user(revel_user_factory, "u2"), notification_type=NotificationType.REFUND_FAILED, context=context
    )

    for text in _channels(notification, NotificationType.REFUND_FAILED)[2:]:
        assert "membership Monthly" in text
        assert "stays cancelled" in text
        assert "must subscribe again" in text
        assert "credit note" not in text


def test_buyer_message_says_the_money_was_not_returned_and_to_contact_the_organizer(revel_user_factory: t.Any) -> None:
    notification = _create_notification_for_test(
        user=_user(revel_user_factory, "u3"),
        notification_type=NotificationType.TICKET_REFUND_FAILED,
        context={
            "ticket_id": "00000000-0000-0000-0000-000000000003",
            "event_id": "00000000-0000-0000-0000-000000000002",
            "event_name": "Summer Fest",
            "organization_id": "00000000-0000-0000-0000-000000000001",
            "refund_amount": "40.00 EUR",
        },
    )

    rendered = _channels(notification, NotificationType.TICKET_REFUND_FAILED)

    assert "Summer Fest" in rendered[0]
    assert "Summer Fest" in rendered[1]
    for text in rendered[2:]:
        assert "40.00 EUR" in text
        assert "did not go through at the payment provider" in text
        assert "not returned" in text
        assert "Contact the organizer" in text
        # Staff delivery is not guaranteed (no eligible recipient), so the buyer is not told staff knows.
        assert "was told" not in text


@pytest.mark.parametrize(
    ("language", "fragment"),
    [
        ("de", "nicht durchgegangen"),
        ("es", "no se ha completado"),
        ("fr", "n'a pas abouti"),
        ("it", "non è andato a buon fine"),
        ("pt", "não foi concluído"),
    ],
)
def test_every_locale_translates_both_messages(language: str, fragment: str, revel_user_factory: t.Any) -> None:
    """The compiled catalogs hold the new strings: no English falls through."""
    staff = _create_notification_for_test(
        user=_user(revel_user_factory, "u4"), notification_type=NotificationType.REFUND_FAILED, context=_STAFF_CONTEXT
    )
    buyer = _create_notification_for_test(
        user=_user(revel_user_factory, "u5"),
        notification_type=NotificationType.TICKET_REFUND_FAILED,
        context={
            "ticket_id": "t",
            "event_id": "00000000-0000-0000-0000-000000000002",
            "event_name": "Summer Fest",
            "organization_id": "00000000-0000-0000-0000-000000000001",
            "refund_amount": "40.00 EUR",
        },
    )
    with translation.override(language):
        for notification, notification_type in (
            (staff, NotificationType.REFUND_FAILED),
            (buyer, NotificationType.TICKET_REFUND_FAILED),
        ):
            template = get_template(notification_type)
            assert fragment in template.get_in_app_body(notification)
            assert fragment in template.get_email_text_body(notification)
            assert "did not go through" not in template.get_in_app_body(notification)


def test_owner_and_staff_with_manage_tickets_are_recipients_and_other_staff_are_not(
    organization: Organization,
    django_user_model: t.Type[RevelUser],
) -> None:
    allowed = django_user_model.objects.create_user(username="a", email="a@example.com", password="pw")
    denied = django_user_model.objects.create_user(username="b", email="b@example.com", password="pw")
    OrganizationStaff.objects.create(organization=organization, user=allowed)
    OrganizationStaff.objects.create(
        organization=organization,
        user=denied,
        permissions=PermissionsSchema(default=PermissionMap(manage_tickets=False)).model_dump(mode="json"),
    )

    recipients = set(get_staff_for_notification(organization.id, NotificationType.REFUND_FAILED))

    assert recipients == {organization.owner, allowed}


def test_staff_membership_message_omits_the_cancelled_sentence_when_no_refund_cancelled_it(
    revel_user_factory: t.Any,
) -> None:
    context = {k: v for k, v in _STAFF_CONTEXT.items() if k not in ("event_id", "event_name", "resolve_url")}
    context.update(
        kind="membership", plan_name="Monthly", credit_note_numbers=[], subscription_cancelled_by_refund=False
    )
    notification = _create_notification_for_test(
        user=_user(revel_user_factory, "u3"), notification_type=NotificationType.REFUND_FAILED, context=context
    )

    for text in _channels(notification, NotificationType.REFUND_FAILED)[2:]:
        assert "membership Monthly" in text
        assert "stays cancelled" not in text


def test_a_title_longer_than_the_column_is_cut_so_the_notification_still_renders(revel_user_factory: t.Any) -> None:
    """An event name of 255 characters made ``Your refund for <event> failed`` overflow ``Notification.title``."""
    notification = _create_notification_for_test(
        user=_user(revel_user_factory, "u6"),
        notification_type=NotificationType.TICKET_REFUND_FAILED,
        context={
            "ticket_id": "00000000-0000-0000-0000-000000000003",
            "event_id": "00000000-0000-0000-0000-000000000002",
            "event_name": "E" * 255,
            "organization_id": "00000000-0000-0000-0000-000000000001",
            "refund_amount": "40.00 EUR",
        },
    )

    dispatch_notification(str(notification.pk))

    notification.refresh_from_db()
    assert len(notification.title) == 255
    assert notification.title.startswith("Your refund for EEE")
    assert "did not go through at the payment provider" in notification.body
