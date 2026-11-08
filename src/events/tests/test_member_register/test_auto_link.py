"""Tests for linking records when an account gets a verified matching email (#1319).

pytest-django rolls the wrapping transaction back, so the ``on_commit`` dispatch only runs inside
``django_capture_on_commit_callbacks(execute=True)``; Celery runs eagerly in tests, so the dispatched task
executes the service in-process.
"""

import typing as t
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import RevelUser
from events.models import MembershipRecord, Organization, OrganizationMember, OrganizationStaff
from events.service.member_register_linking import LinkOutcome, auto_link_records_for_user
from notifications.enums import NotificationType

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

AUTO_LINK_TASK = "events.signals.auto_link_member_records.delay"


def _user(django_user_model: type[RevelUser], email: str = "pat@example.com", **extra: t.Any) -> RevelUser:
    return django_user_model.objects.create_user(username=email, email=email, password="pw", **extra)


def _linked(record: MembershipRecord) -> bool:
    record.refresh_from_db()
    return record.user_id is not None


def test_verification_links_and_alerts_staff(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    record = make_record()
    user = _user(django_user_model)
    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            user.email_verified = True
            user.save()

    record.refresh_from_db()
    assert record.user == user
    assert (record.linked_via, record.linked_at is not None) == (MembershipRecord.LinkMethod.AUTO_EMAIL, True)
    assert OrganizationMember.objects.filter(organization=organization, user=user).exists()
    sent = {c.kwargs["notification_type"]: c.kwargs for c in send.call_args_list}
    assert NotificationType.MEMBERSHIP_GRANTED in sent
    alert = sent[NotificationType.STAFF_MEMBER_RECORD_AUTO_LINKED]
    assert alert["user"] == organization_owner_user
    assert alert["context"]["record_id"] == str(record.pk)
    assert alert["context"]["frontend_url"].endswith(f"/org/{organization.slug}/admin/members?tab=register")


def test_creation_already_verified_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    with django_capture_on_commit_callbacks(execute=True):
        user = _user(django_user_model, email_verified=True)

    assert _linked(record) and record.user == user


def test_verified_email_change_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record(email="new@example.com")
    user = _user(django_user_model, email="old@example.com", email_verified=True)
    with django_capture_on_commit_callbacks(execute=True):
        user.email = "new@example.com"
        user.save()

    assert _linked(record)


def test_guest_conversion_ending_verified_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    user = _user(django_user_model, email_verified=True, guest=True)
    assert not _linked(record)
    with django_capture_on_commit_callbacks(execute=True):
        user.guest = False
        user.save()

    assert _linked(record)


def test_unverified_creation_does_not_link(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    with django_capture_on_commit_callbacks(execute=True):
        _user(django_user_model)

    assert not _linked(record)


def test_guest_never_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    with django_capture_on_commit_callbacks(execute=True):
        user = _user(django_user_model, email_verified=True, guest=True)
        user.email_verified = True
        user.save()

    assert not _linked(record)
    assert auto_link_records_for_user(user.pk) == []


def test_case_insensitive_match_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record(email="Pat@Example.COM")
    with django_capture_on_commit_callbacks(execute=True):
        _user(django_user_model, email="pat@example.com", email_verified=True)

    assert _linked(record)


def test_plus_tag_does_not_match(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record(email="pat@example.com")
    with django_capture_on_commit_callbacks(execute=True):
        _user(django_user_model, email="pat+tag@example.com", email_verified=True)

    assert not _linked(record)


def test_non_matching_email_does_nothing(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    with patch(AUTO_LINK_TASK) as delay, django_capture_on_commit_callbacks(execute=True):
        _user(django_user_model, email="someone@else.org", email_verified=True)

    assert not _linked(record)
    delay.assert_not_called()


def test_unrelated_update_fields_do_not_query_or_dispatch(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    make_record()
    user = _user(django_user_model, email_verified=True)
    with patch(AUTO_LINK_TASK) as delay, django_capture_on_commit_callbacks(execute=True):
        with CaptureQueriesContext(connection) as queries:
            user.save(update_fields=["last_login"])

    delay.assert_not_called()
    assert not [q for q in queries.captured_queries if "events_membershiprecord" in q["sql"]]


def test_already_verified_resave_does_not_dispatch(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    user = _user(django_user_model, email_verified=True)
    make_record()  # matches the already-verified user, who is not re-checked on unrelated saves
    with patch(AUTO_LINK_TASK) as delay, django_capture_on_commit_callbacks(execute=True):
        user.first_name = "Pat"
        user.save()

    delay.assert_not_called()


def test_dispatch_skipped_without_matching_unlinked_record(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    user = _user(django_user_model)
    with patch(AUTO_LINK_TASK) as delay, django_capture_on_commit_callbacks(execute=True):
        user.email_verified = True
        user.save()

    delay.assert_not_called()


def test_duplicate_is_reported_not_linked(
    organization: Organization, make_record: RecordFactory, django_user_model: type[RevelUser]
) -> None:
    user = _user(django_user_model, email_verified=True)
    OrganizationMember.objects.create(organization=organization, user=user)
    record = make_record()

    (result,) = auto_link_records_for_user(user.pk)

    assert result.outcome == LinkOutcome.DUPLICATE
    record.refresh_from_db()
    assert record.user is None and record.duplicate_claimant == user


def test_links_every_matching_record_across_orgs(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    organization_owner_user: RevelUser,
) -> None:
    other_org = Organization.objects.create(name="Other", slug="other", owner=organization_owner_user)
    OrganizationStaff.objects.filter(organization=other_org).delete()
    first = make_record()
    second = make_record(organization=other_org, tier=None, tier_name="")
    user = _user(django_user_model, email_verified=True)

    results = auto_link_records_for_user(user.pk)

    assert [r.outcome for r in results] == [LinkOutcome.LINKED, LinkOutcome.LINKED]
    assert _linked(first) and _linked(second)
