"""Review hardening for register linking (#1319): future starts, token re-check, stale admin instances,
the real account flows that trigger auto-linking, and the auto-link guard rails.

``on_commit`` callbacks only run inside ``django_capture_on_commit_callbacks(execute=True)`` (pytest-django
rolls the wrapping transaction back).
"""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from accounts.service import account as account_service
from events.models import Blacklist, MembershipRecord, MembershipTier, Organization, OrganizationMember
from events.service import member_register_admin_service as admin_service
from events.service import member_register_linking as linking
from events.service.member_register_linking import LinkOutcome, auto_link_records_for_user, link_record
from notifications.enums import NotificationType

from .conftest import RecordFactory, make_client

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus
STAFF_ALERT = NotificationType.STAFF_MEMBER_RECORD_AUTO_LINKED


def _types(send: t.Any) -> list[NotificationType]:
    return [c.kwargs["notification_type"] for c in send.call_args_list]


def _future(days: int = 5) -> t.Any:
    return timezone.now() + timedelta(days=days)


def _verified(django_user_model: type[RevelUser], email: str = "pat@example.com", **extra: t.Any) -> RevelUser:
    return django_user_model.objects.create_user(
        username=email, email=email, password="pw", email_verified=True, **extra
    )


# ---- future start (NOT_STARTED) ----


def test_link_record_not_started_changes_nothing(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    record = make_record(valid_from=_future(), invite_token="tok")

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.NOT_STARTED
    record.refresh_from_db()
    assert record.user is None and record.invite_token == "tok"
    assert not OrganizationMember.objects.filter(organization=organization, user=claimant).exists()


def test_send_invite_refuses_future_record(make_record: RecordFactory) -> None:
    record = make_record(valid_from=_future())

    with pytest.raises(HttpError) as exc:
        linking.send_invite(record)

    assert exc.value.status_code == 400
    record.refresh_from_db()
    assert record.invite_token is None and record.invited_at is None


def test_bulk_invite_skips_future_records(
    organization: Organization,
    make_record: RecordFactory,
    owner_client: Client,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    make_record(email="now@example.com")
    future = make_record(email="later@example.com", valid_from=_future())

    with patch("events.tasks.member_register.send_member_register_invite.delay"):
        with django_capture_on_commit_callbacks(execute=True):
            response = owner_client.post(reverse("api:invite_all_member_register", kwargs={"slug": organization.slug}))

    assert response.json() == {"invited": 1}
    future.refresh_from_db()
    assert future.invite_token is None


def test_claim_not_started_is_409_and_leaves_the_link_usable(make_record: RecordFactory, claimant: RevelUser) -> None:
    record = make_record(valid_from=_future(), invite_token="tok")

    response = make_client(claimant).post(reverse("api:claim_member_register_invite", kwargs={"token": "tok"}))

    assert response.status_code == 409
    assert "hasn't started" in response.json()["detail"]
    record.refresh_from_db()
    assert record.user is None and record.invite_token == "tok"


def test_auto_link_skips_not_started_records(
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    record = make_record(valid_from=_future())
    user = _verified(django_user_model)

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            results = auto_link_records_for_user(user.pk)

    assert results == []
    record.refresh_from_db()
    assert record.user is None
    assert STAFF_ALERT not in _types(send)


# ---- token re-check under lock ----


def test_stale_token_is_treated_as_already_linked(make_record: RecordFactory, claimant: RevelUser) -> None:
    """The caller looked the record up with an old token; a resend rotated it before the lock was taken."""
    record = make_record(invite_token="rotated")

    result = link_record(record, claimant, token="old")

    assert result.outcome == LinkOutcome.ALREADY_LINKED
    record.refresh_from_db()
    assert record.user is None and record.invite_token == "rotated"


def test_claim_with_rotated_token_is_404(make_record: RecordFactory, claimant: RevelUser) -> None:
    record = make_record(invite_token="old")
    with patch.object(linking, "get_record_for_invite_token", return_value=record):
        MembershipRecord.objects.filter(pk=record.pk).update(invite_token="new")  # rotated after the lookup
        response = make_client(claimant).post(reverse("api:claim_member_register_invite", kwargs={"token": "old"}))

    assert response.status_code == 404
    record.refresh_from_db()
    assert record.user is None


# ---- revoked ----


def test_revoked_record_token_is_dead(make_record: RecordFactory, claimant: RevelUser) -> None:
    start = timezone.now() - timedelta(days=3)
    make_record(
        valid_from=start,
        valid_until=start,
        ended_reason=MembershipRecord.EndedReason.REVOKED,
        invite_token="tok",
    )

    assert linking.get_record_for_invite_token("tok") is None
    assert (
        make_client(claimant).post(reverse("api:claim_member_register_invite", kwargs={"token": "tok"})).status_code
        == 404
    )
    assert Client().get(reverse("api:preview_member_register_invite", kwargs={"token": "tok"})).status_code == 404


# ---- stale instances in the admin service ----


def test_delete_with_stale_instance_is_refused_after_a_claim(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    stale = make_record()  # fetched while unlinked: the live-access-row check would find nothing
    link_record(MembershipRecord.objects.get(pk=stale.pk), claimant)

    with pytest.raises(HttpError) as exc:
        admin_service.delete_record(stale)

    assert exc.value.status_code == 409
    assert MembershipRecord.objects.filter(pk=stale.pk, user=claimant).exists()
    assert OrganizationMember.objects.get(organization=organization, user=claimant).status == Status.ACTIVE


def test_end_with_stale_instance_is_refused_after_a_claim(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser
) -> None:
    stale = make_record()
    link_record(MembershipRecord.objects.get(pk=stale.pk), claimant)

    with pytest.raises(HttpError) as exc:
        admin_service.end_record(stale, reason=MembershipRecord.EndedReason.CANCELLED, valid_until=None)

    assert exc.value.status_code == 409
    record = MembershipRecord.objects.get(pk=stale.pk)
    assert record.valid_until is None and record.user == claimant
    assert OrganizationMember.objects.get(organization=organization, user=claimant).status == Status.ACTIVE


# ---- linking details ----


def test_legacy_active_row_at_same_tier_links_without_tier_churn(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser, link_tier: MembershipTier
) -> None:
    OrganizationMember.objects.create(organization=organization, user=claimant, tier=link_tier)
    MembershipRecord.objects.filter(organization=organization, user=claimant).delete()  # legacy: no register row
    record = make_record()

    result = link_record(record, claimant)

    assert result.outcome == LinkOutcome.LINKED
    assert list(MembershipRecord.objects.open().filter(organization=organization, user=claimant)) == [record]
    assert not MembershipRecord.objects.filter(
        organization=organization, ended_reason=MembershipRecord.EndedReason.TIER_CHANGED
    ).exists()


def test_email_only_hard_blacklist_blocks(
    organization: Organization,
    make_record: RecordFactory,
    claimant: RevelUser,
    organization_owner_user: RevelUser,
) -> None:
    Blacklist.objects.create(organization=organization, email=claimant.email, created_by=organization_owner_user)
    OrganizationMember.objects.filter(organization=organization, user=claimant).delete()
    record = make_record()

    assert link_record(record, claimant).outcome == LinkOutcome.BANNED
    record.refresh_from_db()
    assert record.user is None


@pytest.mark.parametrize("kind", [MembershipRecord.Kind.STANDARD, MembershipRecord.Kind.TEMPORARY])
def test_claim_closed_or_temporary_record_via_api_grants_no_access_row(
    organization: Organization, make_record: RecordFactory, claimant: RevelUser, kind: str
) -> None:
    start = timezone.now() - timedelta(days=20)
    extra: dict[str, t.Any] = (
        {"valid_from": start, "valid_until": start + timedelta(days=3)}
        if kind == MembershipRecord.Kind.STANDARD
        else {"kind": kind}
    )
    record = make_record(invite_token="tok", **extra)

    response = make_client(claimant).post(reverse("api:claim_member_register_invite", kwargs={"token": "tok"}))

    assert response.status_code == 200, response.content
    record.refresh_from_db()
    assert record.user == claimant
    assert not OrganizationMember.objects.filter(organization=organization, user=claimant).exists()


def test_duplicate_claim_is_visible_to_staff_in_the_list(
    organization: Organization,
    make_record: RecordFactory,
    claimant: RevelUser,
    owner_client: Client,
    link_tier: MembershipTier,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=claimant, tier=link_tier)
    make_record(email="dup@example.com", invite_token="tok")
    response = make_client(claimant).post(reverse("api:claim_member_register_invite", kwargs={"token": "tok"}))
    assert response.status_code == 409
    assert "staff can review the duplicate entry" in response.json()["detail"]

    results = owner_client.get(reverse("api:list_member_register", kwargs={"slug": organization.slug})).json()[
        "results"
    ]

    flags = {r["email"]: r["has_duplicate_claim"] for r in results}
    assert flags["dup@example.com"] is True
    assert flags[claimant.email] is False


# ---- auto-link guard rails ----


def test_auto_link_banned_row_is_not_linked_and_no_alert(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    user = _verified(django_user_model)
    OrganizationMember.objects.create(organization=organization, user=user, status=Status.BANNED)
    record = make_record()

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            (result,) = auto_link_records_for_user(user.pk)

    assert result.outcome == LinkOutcome.BANNED
    record.refresh_from_db()
    assert record.user is None
    assert STAFF_ALERT not in _types(send)


def test_auto_link_hard_blacklisted_user_is_not_linked_and_no_alert(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    user = _verified(django_user_model)
    Blacklist.objects.create(organization=organization, user=user, created_by=organization_owner_user)
    OrganizationMember.objects.filter(organization=organization, user=user).delete()
    record = make_record()

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            (result,) = auto_link_records_for_user(user.pk)

    assert result.outcome == LinkOutcome.BANNED
    record.refresh_from_db()
    assert record.user is None
    assert STAFF_ALERT not in _types(send)


def test_auto_link_duplicate_flags_record_without_staff_alert(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    user = _verified(django_user_model)
    OrganizationMember.objects.create(organization=organization, user=user)
    record = make_record()

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            (result,) = auto_link_records_for_user(user.pk)

    assert result.outcome == LinkOutcome.DUPLICATE
    record.refresh_from_db()
    assert record.user is None and record.duplicate_claimant == user
    assert STAFF_ALERT not in _types(send)


def test_auto_link_onto_cancelled_row_grants_and_alerts(
    organization: Organization,
    make_record: RecordFactory,
    django_user_model: type[RevelUser],
    organization_owner_user: RevelUser,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    user = _verified(django_user_model)
    OrganizationMember.objects.create(organization=organization, user=user, status=Status.CANCELLED)
    record = make_record()

    with patch("notifications.signals.notification_requested.send") as send:
        with django_capture_on_commit_callbacks(execute=True):
            (result,) = auto_link_records_for_user(user.pk)

    assert result.outcome == LinkOutcome.LINKED
    record.refresh_from_db()
    assert record.user == user
    assert OrganizationMember.objects.get(organization=organization, user=user).status == Status.ACTIVE
    types = _types(send)
    assert NotificationType.MEMBERSHIP_GRANTED in types and STAFF_ALERT in types


# ---- real account flows reach the auto-link ----


def test_verify_email_flow_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    user = django_user_model.objects.create_user(username="pat@example.com", email="pat@example.com", password="pw")
    token = account_service.create_verification_token(user)

    with django_capture_on_commit_callbacks(execute=True):
        account_service.verify_email(token)

    record.refresh_from_db()
    assert record.user == user


def test_confirm_email_change_flow_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record(email="new@example.com")
    user = _verified(django_user_model, email="old@example.com")
    token = account_service.create_email_change_token(user, "new@example.com")

    with patch("accounts.tasks.send_account_email.delay"):
        with django_capture_on_commit_callbacks(execute=True):
            account_service.confirm_email_change(token)

    record.refresh_from_db()
    assert record.user == user


def test_guest_conversion_flow_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    guest = django_user_model.objects.create_user(
        username="pat@example.com", email="pat@example.com", password="pw", guest=True
    )

    with django_capture_on_commit_callbacks(execute=True):
        assert account_service.convert_guest(guest) is True

    record.refresh_from_db()
    assert record.user == guest


def test_update_fields_email_verified_save_links(
    make_record: RecordFactory, django_user_model: type[RevelUser], django_capture_on_commit_callbacks: t.Any
) -> None:
    record = make_record()
    user = django_user_model.objects.create_user(username="pat@example.com", email="pat@example.com", password="pw")

    with django_capture_on_commit_callbacks(execute=True):
        user.email_verified = True
        user.save(update_fields=["email_verified"])

    record.refresh_from_db()
    assert record.user == user


# ---- pre_save stash ----


def test_stash_skips_the_select_for_new_users(django_assert_num_queries: t.Any) -> None:
    from events.signals import stash_previous_link_state

    new_user = RevelUser(username="fresh@example.com", email="fresh@example.com")
    with django_assert_num_queries(0):
        stash_previous_link_state(sender=RevelUser, instance=new_user)
    assert new_user._register_link_prev is None  # type: ignore[attr-defined]


def test_create_user_leaves_no_stash(django_user_model: type[RevelUser]) -> None:
    """A UUID default pk is set before save, so only ``_state.adding`` identifies a new row."""
    user = django_user_model.objects.create_user(username="c@example.com", email="c@example.com", password="pw")
    assert user._register_link_prev is None  # type: ignore[attr-defined]


def test_auto_link_skips_future_closed_record(make_record: RecordFactory, django_user_model: type[RevelUser]) -> None:
    """A closed or TEMPORARY entry that hasn't started is not auto-linked either."""
    start = _future()
    record = make_record(valid_from=start, valid_until=start + timedelta(days=1))
    user = _verified(django_user_model)

    assert auto_link_records_for_user(user.pk) == []
    record.refresh_from_db()
    assert record.user is None


def test_auto_link_rechecks_email_under_lock(make_record: RecordFactory, django_user_model: type[RevelUser]) -> None:
    """If the user's email changed after the records were selected, the automatic link changes nothing."""
    record = make_record()
    user = _verified(django_user_model)
    RevelUser.objects.filter(pk=user.pk).update(email="moved@example.com")

    result = link_record(record, user, require_verified_email=True)

    assert result.outcome == LinkOutcome.ALREADY_LINKED
    record.refresh_from_db()
    assert record.user is None


def test_auto_link_rechecks_verification_under_lock(
    make_record: RecordFactory, django_user_model: type[RevelUser]
) -> None:
    """An email that lost its verification in the meantime no longer links."""
    record = make_record()
    user = _verified(django_user_model)
    RevelUser.objects.filter(pk=user.pk).update(email_verified=False)

    assert link_record(record, user, require_verified_email=True).outcome == LinkOutcome.ALREADY_LINKED
    record.refresh_from_db()
    assert record.user is None


def test_revocation_is_rechecked_under_the_lock(make_record: RecordFactory, claimant: RevelUser) -> None:
    """A record revoked between the token lookup and the lock can't be claimed."""
    start = timezone.now() - timedelta(days=2)
    record = make_record(
        kind=MembershipRecord.Kind.TEMPORARY,
        valid_from=start,
        valid_until=start + timedelta(days=1),
        ended_reason=MembershipRecord.EndedReason.REVOKED,
        invite_token="tok",
    )

    assert link_record(record, claimant, token="tok").outcome == LinkOutcome.ALREADY_LINKED
    record.refresh_from_db()
    assert record.user is None


def test_email_change_invalidates_the_mailed_link(make_record: RecordFactory) -> None:
    """Correcting an invited entry's email kills the old link and resets the invite state."""
    record = make_record(invite_token="old", invited_at=timezone.now())

    admin_service.update_record(record, {"email": "corrected@example.com"})

    record.refresh_from_db()
    assert record.invite_token is None and record.invited_at is None
    assert linking.get_record_for_invite_token("old") is None


def test_same_email_edit_keeps_the_link(make_record: RecordFactory) -> None:
    """A case-only or unchanged email keeps the outstanding invitation."""
    record = make_record(invite_token="keep", invited_at=timezone.now())

    admin_service.update_record(record, {"email": "PAT@example.com", "full_name": "Pat R."})

    record.refresh_from_db()
    assert record.invite_token == "keep" and record.invited_at is not None
