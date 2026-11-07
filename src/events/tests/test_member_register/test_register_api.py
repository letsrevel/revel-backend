"""Tests for the membership register admin API (#1319)."""

import typing as t
from datetime import timedelta

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
)
from events.service import organization_service

pytestmark = pytest.mark.django_db

Status = OrganizationMember.MembershipStatus
Reason = MembershipRecord.EndedReason


def _client(user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    return _client(organization_owner_user)


@pytest.fixture
def staff_client(organization_staff_user: RevelUser, organization: Organization) -> Client:
    """Staff with manage_members."""
    OrganizationStaff.objects.create(
        organization=organization,
        user=organization_staff_user,
        permissions={"default": {"manage_members": True}},
    )
    return _client(organization_staff_user)


@pytest.fixture
def plain_staff_client(organization_staff_user: RevelUser, organization: Organization) -> Client:
    """Staff without manage_members."""
    OrganizationStaff.objects.create(
        organization=organization,
        user=organization_staff_user,
        permissions={"default": {"manage_members": False}},
    )
    return _client(organization_staff_user)


@pytest.fixture
def outsider_client(django_user_model: type[RevelUser]) -> Client:
    return _client(django_user_model.objects.create_user(username="out", email="out@example.com", password="pw"))


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.fixture
def tier2(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Silver")


def _url(name: str, organization: Organization, record: MembershipRecord | None = None) -> str:
    kwargs: dict[str, t.Any] = {"slug": organization.slug}
    if record is not None:
        kwargs["record_id"] = record.pk
    return reverse(f"api:{name}", kwargs=kwargs)


def _rec(
    organization: Organization,
    *,
    name: str = "Ann",
    tier: MembershipTier | None = None,
    valid_from: t.Any = None,
    valid_until: t.Any = None,
    **kw: t.Any,
) -> MembershipRecord:
    kw.setdefault("source", MembershipRecord.Source.MANUAL)
    return MembershipRecord.objects.create(
        organization=organization,
        full_name=name,
        tier=tier,
        tier_name=tier.name if tier else "",
        valid_from=valid_from or timezone.now() - timedelta(days=10),
        valid_until=valid_until,
        **kw,
    )


# ---- list ----


def test_list_shape_pagination_and_backfill(
    owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    _rec(organization, name="Ann", tier=tier, source=MembershipRecord.Source.BACKFILL)
    r = owner_client.get(_url("list_member_register", organization))
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    item = body["results"][0]
    assert item["source"] == "backfill"
    assert item["has_account"] is False
    assert item["tier_id"] == str(tier.pk)
    assert item["user_id"] is None


def test_list_filters(
    owner_client: Client,
    organization: Organization,
    tier: MembershipTier,
    tier2: MembershipTier,
    member_user: RevelUser,
) -> None:
    now = timezone.now()
    a = _rec(organization, name="Ann", tier=tier)
    b = _rec(
        organization,
        name="Bob",
        tier=tier2,
        kind=MembershipRecord.Kind.TEMPORARY,
        valid_until=now - timedelta(days=1),
        ended_reason=Reason.CANCELLED,
    )
    c = _rec(organization, name="Cy", tier=tier, user=member_user)
    url = _url("list_member_register", organization)

    def ids(**params: t.Any) -> set[str]:
        resp = owner_client.get(url, params)
        assert resp.status_code == 200, resp.content
        return {i["id"] for i in resp.json()["results"]}

    assert ids(kind="temporary") == {str(b.pk)}
    assert ids(tier_id=str(tier.pk)) == {str(a.pk), str(c.pk)}
    assert ids(is_open="true") == {str(a.pk), str(c.pk)}
    assert ids(is_open="false") == {str(b.pk)}
    assert ids(has_account="true") == {str(c.pk)}
    assert ids(has_account="false") == {str(a.pk), str(b.pk)}
    assert ids(search="bob") == {str(b.pk)}


def test_list_active_on(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    now = timezone.now()
    when = now - timedelta(days=2)
    open_rec = _rec(organization, name="Open", tier=tier)
    revoked = _rec(
        organization, name="Rev", tier=tier, valid_until=now - timedelta(days=1), ended_reason=Reason.REVOKED
    )
    boundary = _rec(organization, name="Edge", tier=tier, valid_until=when, ended_reason=Reason.CANCELLED)
    covering = _rec(
        organization, name="Cov", tier=tier, valid_until=now - timedelta(days=1), ended_reason=Reason.CANCELLED
    )
    resp = owner_client.get(_url("list_member_register", organization), {"active_on": when.isoformat()})
    got = {i["id"] for i in resp.json()["results"]}
    assert got == {str(open_rec.pk), str(covering.pk)}
    assert str(revoked.pk) not in got and str(boundary.pk) not in got


def test_list_permissions_and_isolation(
    plain_staff_client: Client, outsider_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    _rec(organization, tier=tier)
    assert plain_staff_client.get(_url("list_member_register", organization)).status_code == 403
    assert outsider_client.get(_url("list_member_register", organization)).status_code in (403, 404)


def test_other_org_records_not_visible(
    owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    other = Organization.objects.create(name="Other", slug="other", owner=organization_owner_user)
    foreign = _rec(other, name="Foreign")
    assert owner_client.get(_url("list_member_register", organization)).json()["count"] == 0
    assert owner_client.get(_url("get_member_register_record", organization, foreign)).status_code == 404


def test_get_record(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    rec = _rec(organization, tier=tier)
    r = owner_client.get(_url("get_member_register_record", organization, rec))
    assert r.status_code == 200
    assert r.json()["id"] == str(rec.pk)


# ---- create ----


def _create_payload(tier: MembershipTier, **over: t.Any) -> dict[str, t.Any]:
    payload: dict[str, t.Any] = {
        "full_name": "  Jane Roe ",
        "email": "jane@example.com",
        "tier_id": str(tier.pk),
        "valid_from": (timezone.now() - timedelta(days=3)).isoformat(),
    }
    payload.update(over)
    return payload


def test_create_happy_path(
    staff_client: Client, organization: Organization, tier: MembershipTier, organization_staff_user: RevelUser
) -> None:
    r = staff_client.post(
        _url("create_member_register_record", organization), _create_payload(tier), content_type="application/json"
    )
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["source"] == "manual"
    assert body["full_name"] == "Jane Roe"
    assert body["tier_name"] == "Gold"
    assert body["user_id"] is None
    rec = MembershipRecord.objects.get(pk=body["id"])
    assert rec.created_by == organization_staff_user
    assert rec.kind == MembershipRecord.Kind.STANDARD
    assert rec.ended_reason is None


def test_create_two_open_accountless_allowed(
    owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    for _i in range(2):
        r = owner_client.post(
            _url("create_member_register_record", organization),
            _create_payload(tier),
            content_type="application/json",
        )
        assert r.status_code == 201
    assert MembershipRecord.objects.filter(organization=organization, valid_until__isnull=True).count() == 2


def test_create_foreign_tier_404(
    owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    other = Organization.objects.create(name="Other", slug="other", owner=organization_owner_user)
    foreign_tier = MembershipTier.objects.create(organization=other, name="X")
    r = owner_client.post(
        _url("create_member_register_record", organization),
        _create_payload(foreign_tier),
        content_type="application/json",
    )
    assert r.status_code == 404


def test_create_bad_range(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    payload = _create_payload(tier, valid_until=(timezone.now() - timedelta(days=30)).isoformat())
    r = owner_client.post(_url("create_member_register_record", organization), payload, content_type="application/json")
    assert r.status_code == 400


def test_create_invalid_email_and_blank_name(
    owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    url = _url("create_member_register_record", organization)
    r = owner_client.post(url, _create_payload(tier, email="nope"), content_type="application/json")
    assert r.status_code == 422
    r = owner_client.post(url, _create_payload(tier, full_name="   "), content_type="application/json")
    assert r.status_code == 422


def test_create_service_range_check(
    organization: Organization, tier: MembershipTier, organization_owner_user: RevelUser
) -> None:
    from ninja.errors import HttpError

    from events.service import member_register_admin_service as svc

    now = timezone.now()
    with pytest.raises(HttpError) as exc:
        svc.create_manual_record(
            organization,
            full_name="A",
            email=None,
            tier_id=tier.pk,
            valid_from=now,
            valid_until=now - timedelta(days=1),
            created_by=organization_owner_user,
        )
    assert exc.value.status_code == 400


# ---- update ----


def _patch(client: Client, org: Organization, rec: MembershipRecord, body: dict[str, t.Any]) -> t.Any:
    return client.patch(_url("update_member_register_record", org, rec), body, content_type="application/json")


def test_update_unlinked_full_edit(
    owner_client: Client, organization: Organization, tier: MembershipTier, tier2: MembershipTier
) -> None:
    rec = _rec(organization, tier=tier)
    start = timezone.now() - timedelta(days=20)
    end = timezone.now() - timedelta(days=5)
    r = _patch(
        owner_client,
        organization,
        rec,
        {
            "full_name": "New Name",
            "email": "new@example.com",
            "tier_id": str(tier2.pk),
            "valid_from": start.isoformat(),
            "valid_until": end.isoformat(),
        },
    )
    assert r.status_code == 200, r.content
    rec.refresh_from_db()
    assert rec.full_name == "New Name"
    assert rec.email == "new@example.com"
    assert rec.tier == tier2 and rec.tier_name == "Silver"
    assert rec.valid_until == end


def test_update_range_violation(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    rec = _rec(organization, tier=tier)
    r = _patch(owner_client, organization, rec, {"valid_until": (rec.valid_from - timedelta(days=1)).isoformat()})
    assert r.status_code == 400


def test_update_empty_body_noop(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    rec = _rec(organization, tier=tier)
    assert _patch(owner_client, organization, rec, {}).status_code == 200


def test_update_reopen_unlinked_clears_reason(
    owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    rec = _rec(organization, tier=tier, valid_until=timezone.now() - timedelta(days=1), ended_reason=Reason.CANCELLED)
    r = _patch(owner_client, organization, rec, {"valid_until": None})
    assert r.status_code == 200, r.content
    rec.refresh_from_db()
    assert rec.valid_until is None and rec.ended_reason is None


def test_update_null_required_fields_422(
    owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    rec = _rec(organization, tier=tier)
    for field in ("full_name", "tier_id", "valid_from"):
        assert _patch(owner_client, organization, rec, {field: None}).status_code == 422


def test_update_linked_open(
    owner_client: Client,
    organization: Organization,
    member_user: RevelUser,
    tier: MembershipTier,
    tier2: MembershipTier,
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)
    rec = MembershipRecord.objects.get(organization=organization, user=member_user)
    r = _patch(
        owner_client,
        organization,
        rec,
        {
            "full_name": "Legal Name",
            "email": "x@example.com",
            "valid_from": (rec.valid_from - timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 200, r.content
    assert _patch(owner_client, organization, rec, {"tier_id": str(tier2.pk)}).status_code == 409
    assert _patch(owner_client, organization, rec, {"valid_until": timezone.now().isoformat()}).status_code == 409


def test_update_linked_closed_reopen_409(
    owner_client: Client, organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    rec = _rec(
        organization,
        tier=tier,
        user=member_user,
        valid_until=timezone.now() - timedelta(days=1),
        ended_reason=Reason.CANCELLED,
    )
    assert _patch(owner_client, organization, rec, {"valid_until": None}).status_code == 409


# ---- end ----


def _end(client: Client, org: Organization, rec: MembershipRecord, body: dict[str, t.Any]) -> t.Any:
    return client.post(_url("end_member_register_record", org, rec), body, content_type="application/json")


def test_end_unlinked_default_now_and_given(
    owner_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    rec = _rec(organization, tier=tier)
    r = _end(owner_client, organization, rec, {"reason": "cancelled"})
    assert r.status_code == 200, r.content
    rec.refresh_from_db()
    assert rec.valid_until is not None and rec.ended_reason == Reason.CANCELLED

    rec2 = _rec(organization, name="B", tier=tier)
    when = timezone.now() + timedelta(days=5)
    r = _end(owner_client, organization, rec2, {"reason": "removed", "valid_until": when.isoformat()})
    assert r.status_code == 200
    rec2.refresh_from_db()
    assert rec2.valid_until == when and rec2.ended_reason == Reason.REMOVED


def test_end_already_ended_409(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    rec = _rec(organization, tier=tier, valid_until=timezone.now() - timedelta(days=1), ended_reason=Reason.CANCELLED)
    assert _end(owner_client, organization, rec, {"reason": "cancelled"}).status_code == 409


def test_end_bad_reason_422(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    rec = _rec(organization, tier=tier)
    assert _end(owner_client, organization, rec, {"reason": "expelled"}).status_code == 422


def test_end_range_violation_400(owner_client: Client, organization: Organization, tier: MembershipTier) -> None:
    rec = _rec(organization, tier=tier)
    body = {"reason": "cancelled", "valid_until": (rec.valid_from - timedelta(days=1)).isoformat()}
    assert _end(owner_client, organization, rec, body).status_code == 400


@pytest.mark.parametrize("status", [Status.ACTIVE, Status.PAUSED])
def test_end_linked_cancels_member(
    owner_client: Client,
    organization: Organization,
    member_user: RevelUser,
    tier: MembershipTier,
    status: str,
) -> None:
    member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier, status=status)
    rec = MembershipRecord.objects.get(organization=organization, user=member_user)
    r = _end(owner_client, organization, rec, {"reason": "removed"})
    assert r.status_code == 200, r.content
    member.refresh_from_db()
    assert member.status == Status.CANCELLED
    rec.refresh_from_db()
    assert rec.ended_reason == Reason.REMOVED and rec.valid_until is not None
    member.save()
    assert not MembershipRecord.objects.filter(
        organization=organization, user=member_user, valid_until__isnull=True
    ).exists()


def test_end_linked_future_400(
    owner_client: Client, organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)
    rec = MembershipRecord.objects.get(organization=organization, user=member_user)
    body = {"reason": "cancelled", "valid_until": (timezone.now() + timedelta(days=3)).isoformat()}
    assert _end(owner_client, organization, rec, body).status_code == 400


# ---- delete ----


def test_delete_owner_ok_staff_forbidden(
    owner_client: Client, staff_client: Client, organization: Organization, tier: MembershipTier
) -> None:
    rec = _rec(organization, tier=tier)
    assert staff_client.delete(_url("delete_member_register_record", organization, rec)).status_code == 403
    assert owner_client.delete(_url("delete_member_register_record", organization, rec)).status_code == 204
    assert not MembershipRecord.objects.filter(pk=rec.pk).exists()


def test_delete_linked_open_409_then_ok_after_removal(
    owner_client: Client, organization: Organization, member_user: RevelUser, tier: MembershipTier
) -> None:
    OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)
    rec = MembershipRecord.objects.get(organization=organization, user=member_user)
    url = _url("delete_member_register_record", organization, rec)
    assert owner_client.delete(url).status_code == 409
    organization_service.remove_member(organization, member_user)
    rec.refresh_from_db()
    assert rec.valid_until is not None
    assert owner_client.delete(url).status_code == 204
