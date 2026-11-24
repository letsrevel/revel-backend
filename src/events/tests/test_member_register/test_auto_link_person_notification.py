"""The person is told when a verified email links them to member-register entries (#1495).

The notification is sent from ``transaction.on_commit``; pytest-django rolls the wrapping transaction back,
so these tests run the callbacks with ``django_capture_on_commit_callbacks(execute=True)``.
"""

import typing as t
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from django.template.loader import render_to_string
from django.utils import timezone, translation

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization, OrganizationMember, OrganizationStaff
from events.service.member_register_linking import LinkOutcome, auto_link_records_for_user, claim_invite
from notifications.enums import DeliveryChannel, NotificationType
from notifications.models import Notification
from notifications.service.dispatcher import determine_delivery_channels

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

LINKED = NotificationType.MEMBER_RECORD_LINKED
GRANTED = NotificationType.MEMBERSHIP_GRANTED
SEND = "notifications.signals.notification_requested.send"


def _user(django_user_model: type[RevelUser], **extra: t.Any) -> RevelUser:
    return django_user_model.objects.create_user(
        username="pat@example.com", email="pat@example.com", password="pw", email_verified=True, **extra
    )


def _person_notifications(send: MagicMock) -> list[t.Mapping[str, t.Any]]:
    return [c.kwargs for c in send.call_args_list if c.kwargs["notification_type"] == LINKED]


def _other_org(owner: RevelUser) -> Organization:
    other = Organization.objects.create(name="Other Club", slug="other-club", owner=owner)
    OrganizationStaff.objects.filter(organization=other).delete()
    return other


def test_one_notification_per_linked_organization(
    organization: Organization,
    organization_owner_user: RevelUser,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    other = _other_org(organization_owner_user)
    now = timezone.now()
    make_record(valid_from=now - timedelta(days=60), valid_until=now - timedelta(days=30))  # closed, same org
    make_record()
    make_record(organization=other, tier=None, tier_name="")
    user = _user(django_user_model)

    with patch(SEND) as send, django_capture_on_commit_callbacks(execute=True):
        results = auto_link_records_for_user(user.pk)

    assert [r.outcome for r in results] == [LinkOutcome.LINKED] * 3
    sent = _person_notifications(send)
    assert sorted(n["context"]["organization_id"] for n in sent) == sorted([str(organization.pk), str(other.pk)])
    assert all(n["user"] == user for n in sent)
    by_org = {n["context"]["organization_id"]: n["context"] for n in sent}
    assert by_org[str(organization.pk)]["organization_name"] == organization.name
    assert by_org[str(organization.pk)]["frontend_url"].endswith(f"/org/{organization.slug}")
    assert by_org[str(other.pk)]["frontend_url"].endswith("/org/other-club")


@pytest.mark.parametrize("kind", ["closed", "temporary"])
def test_no_notification_when_the_link_gives_no_open_membership(
    kind: str,
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    now = timezone.now()
    if kind == "closed":
        make_record(valid_from=now - timedelta(days=60), valid_until=now - timedelta(days=30))
    else:
        make_record(kind=MembershipRecord.Kind.TEMPORARY, valid_until=now + timedelta(days=1))
    user = _user(django_user_model)

    with patch(SEND) as send, django_capture_on_commit_callbacks(execute=True):
        results = auto_link_records_for_user(user.pk)

    assert [r.outcome for r in results] == [LinkOutcome.LINKED]
    assert not OrganizationMember.objects.filter(organization=organization, user=user).exists()
    assert _person_notifications(send) == []
    types = [c.kwargs["notification_type"] for c in send.call_args_list]
    assert NotificationType.STAFF_MEMBER_RECORD_AUTO_LINKED in types  # staff are still told


def test_membership_granted_is_suppressed_only_on_the_auto_link(
    organization: Organization,
    organization_owner_user: RevelUser,
    make_record: RecordFactory,
    claimant: RevelUser,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    make_record()
    user = _user(django_user_model)
    with patch(SEND) as send, django_capture_on_commit_callbacks(execute=True):
        auto_link_records_for_user(user.pk)
    auto_types = [c.kwargs["notification_type"] for c in send.call_args_list]
    assert NotificationType.MEMBERSHIP_GRANTED not in auto_types
    assert auto_types.count(LINKED) == 1

    # An invite claim and a plain grant still send MEMBERSHIP_GRANTED (the suppression does not leak).
    other = _other_org(organization_owner_user)
    make_record(organization=other, tier=None, tier_name="", email="x@example.com", invite_token="tok-1495")
    plain = django_user_model.objects.create_user(
        username="plain@example.com", email="plain@example.com", password="pw"
    )
    with patch(SEND) as send, django_capture_on_commit_callbacks(execute=True):
        result = claim_invite("tok-1495", claimant)
        OrganizationMember.objects.create(organization=organization, user=plain)
    assert result is not None and result.outcome == LinkOutcome.LINKED
    granted = [c.kwargs["user"] for c in send.call_args_list if c.kwargs["notification_type"] == GRANTED]
    assert sorted(u.pk for u in granted) == sorted([claimant.pk, plain.pk])
    assert _person_notifications(send) == []


@pytest.mark.parametrize(
    ("status", "notified"),
    [
        (OrganizationMember.MembershipStatus.ACTIVE, True),
        (OrganizationMember.MembershipStatus.PAUSED, False),
        (OrganizationMember.MembershipStatus.AWAITING_PAYMENT, False),
    ],
)
def test_existing_access_row_is_notified_only_when_active(
    status: OrganizationMember.MembershipStatus,
    notified: bool,
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    user = _user(django_user_model)
    OrganizationMember.objects.create(organization=organization, user=user, status=status)
    MembershipRecord.objects.filter(organization=organization, user=user).delete()  # legacy: no register row
    make_record()

    with patch(SEND) as send, django_capture_on_commit_callbacks(execute=True):
        results = auto_link_records_for_user(user.pk)

    assert [r.outcome for r in results] == [LinkOutcome.LINKED]
    assert len(_person_notifications(send)) == (1 if notified else 0)
    types = [c.kwargs["notification_type"] for c in send.call_args_list]
    assert GRANTED not in types


def test_no_notification_when_nothing_is_linked(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    make_record(email="someone@else.org")  # no match
    user = _user(django_user_model)
    OrganizationMember.objects.create(organization=organization, user=user)
    make_record()  # matches, but the user already holds an open entry: DUPLICATE, not linked
    with patch(SEND) as send, django_capture_on_commit_callbacks(execute=True):
        results = auto_link_records_for_user(user.pk)

    assert [r.outcome for r in results] == [LinkOutcome.DUPLICATE]
    assert _person_notifications(send) == []


def test_banned_outcome_sends_nothing(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    user = _user(django_user_model)
    OrganizationMember.objects.create(
        organization=organization, user=user, status=OrganizationMember.MembershipStatus.BANNED
    )
    make_record()
    with patch(SEND) as send, django_capture_on_commit_callbacks(execute=True):
        results = auto_link_records_for_user(user.pk)

    assert [r.outcome for r in results] == [LinkOutcome.BANNED]
    assert _person_notifications(send) == []


def test_a_failed_send_does_not_stop_the_other_organizations(
    organization: Organization,
    organization_owner_user: RevelUser,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    other = _other_org(organization_owner_user)
    make_record()
    make_record(organization=other, tier=None, tier_name="")
    user = _user(django_user_model)
    calls: list[str] = []

    def send(**kwargs: t.Any) -> None:
        if kwargs["notification_type"] != LINKED:
            return
        calls.append(kwargs["context"]["organization_id"])
        if len(calls) == 1:
            raise RuntimeError("boom")

    with patch(SEND, side_effect=send), django_capture_on_commit_callbacks(execute=True):
        auto_link_records_for_user(user.pk)

    assert sorted(calls) == sorted([str(organization.pk), str(other.pk)])


def test_notification_renders_in_recipient_language_with_organization_link(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    """Celery has no active language: the worker runs under "en" and must render in the recipient's language."""
    make_record()
    user = _user(django_user_model, language="de")

    with translation.override("en"), django_capture_on_commit_callbacks(execute=True):
        auto_link_records_for_user(user.pk)

    notification = Notification.objects.get(user=user, notification_type=LINKED)
    with translation.override("de"):
        title = translation.gettext("Your account is now linked to the member register of %(org)s") % {
            "org": organization.name
        }
        contact = translation.gettext(
            "If this entry is not yours, or you do not want to be a member, contact the organization."
        )
    assert title != f"Your account is now linked to the member register of {organization.name}"
    assert notification.title == title
    assert contact in notification.body
    assert f"/org/{organization.slug}" in notification.body


def test_respects_a_disabled_preference(django_user_model: type[RevelUser]) -> None:
    user = _user(django_user_model)
    assert DeliveryChannel.EMAIL in determine_delivery_channels(user, LINKED)

    prefs = user.notification_preferences
    prefs.notification_type_settings[LINKED] = {"enabled": False}
    prefs.save(update_fields=["notification_type_settings"])
    user.refresh_from_db()

    assert determine_delivery_channels(user, LINKED) == []


@pytest.mark.parametrize(
    "template",
    [
        "notifications/email/member_record_linked.html",
        "notifications/email/member_record_linked.txt",
        "notifications/in_app/member_record_linked.md",
        "notifications/telegram/member_record_linked.md",
    ],
)
def test_every_channel_renders_the_organization_link(template: str) -> None:
    url = "https://platform.example/org/acme-club"
    context = {"organization_name": "Acme Club", "frontend_url": url}

    with translation.override("en"):
        body = render_to_string(template, {"user": {"display_name": "Sam"}, "context": context})

    assert "Acme Club keeps a member register" in body
    assert "contact the organization" in body
    assert url in body
    assert "{{" not in body and "None" not in body
