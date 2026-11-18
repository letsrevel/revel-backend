"""Personal membership invitations: staff invite an account, the account becomes a member by accepting."""

import datetime as dt
import typing as t
from unittest.mock import patch

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.models import (
    Blacklist,
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    OrganizationToken,
)
from events.service import organization_service
from events.utils import format_organization_datetime
from notifications.enums import NotificationType

pytestmark = pytest.mark.django_db

PRIVATE_KEYS = {"email", "phone_number", "first_name", "last_name", "username"}


def _client(user: RevelUser) -> Client:
    return Client(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")  # type: ignore[attr-defined]


def _list_url(org: Organization) -> str:
    return reverse("api:list_member_invitations", kwargs={"slug": org.slug})


def _invite(client: Client, org: Organization, user: RevelUser, tier: MembershipTier) -> t.Any:
    return client.post(
        _list_url(org),
        data=orjson.dumps({"user_id": str(user.pk), "tier_id": str(tier.pk)}),
        content_type="application/json",
    )


def _claim(user: RevelUser, token_id: t.Any) -> t.Any:
    url = reverse("api:organization_claim_invitation", kwargs={"token": str(token_id)})
    return _client(user).post(url)


@pytest.fixture(autouse=True)
def _verified_invitee(nonmember_user: RevelUser) -> None:
    """Invitees must have a verified email; individual tests flip it back to cover the refusal."""
    RevelUser.objects.filter(pk=nonmember_user.pk).update(email_verified=True)


@pytest.fixture
def tier(organization: Organization) -> MembershipTier:
    return MembershipTier.objects.create(organization=organization, name="Gold")


class TestInvite:
    def test_invites_stranger_without_creating_membership(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        response = _invite(organization_owner_client, organization, nonmember_user, tier)

        assert response.status_code == 201, response.content
        body = response.json()
        assert set(body) == {
            "id",
            "invitee_id",
            "invitee_display_name",
            "tier_id",
            "tier_name",
            "expires_at",
            "created_at",
        }
        assert body["invitee_id"] == str(nonmember_user.pk)
        assert body["tier_name"] == "Gold"
        assert not PRIVATE_KEYS & set(body)
        assert nonmember_user.email not in response.content.decode()
        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
        assert not MembershipRecord.objects.filter(organization=organization, user=nonmember_user).exists()
        token = OrganizationToken.objects.get(pk=body["id"])
        assert token.invitee_id == nonmember_user.pk
        assert (token.grants_membership, token.grants_staff_status, token.max_uses) == (True, False, 1)
        assert token.expires_at is not None
        assert dt.timedelta(days=13) < token.expires_at - timezone.now() <= dt.timedelta(days=14)

    def test_notifies_invitee_on_commit(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        with (
            patch("notifications.signals.notification_requested.send") as send,
            django_capture_on_commit_callbacks(execute=True),
        ):
            response = _invite(organization_owner_client, organization, nonmember_user, tier)

        invitations = [
            c
            for c in send.call_args_list
            if c.kwargs["notification_type"] == NotificationType.MEMBERSHIP_INVITATION_RECEIVED
        ]
        assert len(invitations) == 1
        assert invitations[0].kwargs["user"] == nonmember_user
        context = invitations[0].kwargs["context"]
        assert context["accept_url"].endswith(f"/join/org/{response.json()['id']}")
        assert context["tier_name"] == "Gold"
        token = OrganizationToken.objects.get(pk=response.json()["id"])
        assert token.expires_at is not None
        assert context["expires_at"] == format_organization_datetime(token.expires_at, organization)

    def test_invitee_accepts_and_becomes_member(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        token_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]

        response = _claim(nonmember_user, token_id)

        assert response.status_code == 200, response.content
        member = OrganizationMember.objects.get(organization=organization, user=nonmember_user)
        assert member.status == OrganizationMember.MembershipStatus.ACTIVE
        assert member.tier == tier
        record = MembershipRecord.objects.get(organization=organization, user=nonmember_user)
        assert record.source == MembershipRecord.Source.TOKEN
        # Accepted invitations are no longer pending.
        assert organization_owner_client.get(_list_url(organization)).json()["count"] == 0

    def test_other_account_cannot_accept(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        public_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        token_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]

        response = _claim(public_user, token_id)

        assert response.status_code == 400
        assert not OrganizationMember.objects.filter(organization=organization, user=public_user).exists()
        assert not MembershipRecord.objects.filter(organization=organization, user=public_user).exists()
        # The invitee can still accept afterwards.
        assert _claim(nonmember_user, token_id).status_code == 200

    def test_expired_invitation_cannot_be_accepted_or_listed(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        token_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]
        OrganizationToken.objects.filter(pk=token_id).update(expires_at=timezone.now() - dt.timedelta(minutes=1))

        assert _claim(nonmember_user, token_id).status_code == 400
        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()
        assert organization_owner_client.get(_list_url(organization)).json()["count"] == 0

    def test_reinvite_refreshes_same_row_and_notifies_again(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        first_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]
        other_tier = MembershipTier.objects.create(organization=organization, name="Silver")
        OrganizationToken.objects.filter(pk=first_id).update(expires_at=timezone.now() - dt.timedelta(days=1), uses=1)

        with (
            patch("notifications.signals.notification_requested.send") as send,
            django_capture_on_commit_callbacks(execute=True),
        ):
            response = _invite(organization_owner_client, organization, nonmember_user, other_tier)

        assert response.status_code == 201
        assert response.json()["id"] == first_id
        token = OrganizationToken.objects.get(pk=first_id)
        assert (token.uses, token.membership_tier_id) == (0, other_tier.pk)
        assert token.expires_at is not None and token.expires_at > timezone.now() + dt.timedelta(days=13)
        assert OrganizationToken.objects.filter(organization=organization, invitee=nonmember_user).count() == 1
        assert send.call_count == 1

    def test_reinvite_within_cooldown_refreshes_without_notifying(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        first_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]
        other_tier = MembershipTier.objects.create(organization=organization, name="Silver")
        OrganizationToken.objects.filter(pk=first_id).update(expires_at=timezone.now() + dt.timedelta(days=1))

        with (
            patch("notifications.signals.notification_requested.send") as send,
            django_capture_on_commit_callbacks(execute=True),
        ):
            response = _invite(organization_owner_client, organization, nonmember_user, other_tier)

        assert response.status_code == 201
        token = OrganizationToken.objects.get(pk=first_id)
        assert token.membership_tier_id == other_tier.pk
        assert token.expires_at is not None and token.expires_at > timezone.now() + dt.timedelta(days=13)
        assert send.call_count == 0

    def test_reinvite_after_cooldown_notifies_again(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        first_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]
        OrganizationToken.objects.filter(pk=first_id).update(
            invitation_notified_at=timezone.now() - dt.timedelta(hours=25)
        )

        with (
            patch("notifications.signals.notification_requested.send") as send,
            django_capture_on_commit_callbacks(execute=True),
        ):
            _invite(organization_owner_client, organization, nonmember_user, tier)

        assert send.call_count == 1
        token = OrganizationToken.objects.get(pk=first_id)
        assert token.invitation_notified_at is not None
        assert timezone.now() - token.invitation_notified_at < dt.timedelta(minutes=1)

    def test_unverified_invitee_is_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        RevelUser.objects.filter(pk=nonmember_user.pk).update(email_verified=False)

        assert _invite(organization_owner_client, organization, nonmember_user, tier).status_code == 404
        assert not OrganizationToken.objects.filter(invitee=nonmember_user).exists()

    def test_already_member_refused_in_any_status(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        OrganizationMember.objects.create(
            organization=organization, user=nonmember_user, status=OrganizationMember.MembershipStatus.BANNED
        )

        response = _invite(organization_owner_client, organization, nonmember_user, tier)

        assert response.status_code == 400
        assert response.json()["code"] == "already_member"
        assert not OrganizationToken.objects.filter(invitee=nonmember_user).exists()

    def test_hard_blacklisted_refused(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        Blacklist.objects.create(
            organization=organization, user=nonmember_user, reason="x", created_by=organization_owner_user
        )

        response = _invite(organization_owner_client, organization, nonmember_user, tier)

        assert response.status_code == 400
        assert not OrganizationToken.objects.filter(invitee=nonmember_user).exists()

    def test_tier_of_other_org_is_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        other = Organization.objects.create(name="Other", slug="other", owner=organization_owner_user)
        other_tier = MembershipTier.objects.create(organization=other, name="X")

        assert _invite(organization_owner_client, organization, nonmember_user, other_tier).status_code == 404

    def test_inactive_account_is_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        RevelUser.objects.filter(pk=nonmember_user.pk).update(is_active=False)

        assert _invite(organization_owner_client, organization, nonmember_user, tier).status_code == 404


class TestListAndRevoke:
    def test_list_shows_display_name_only(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        _invite(organization_owner_client, organization, nonmember_user, tier)

        data = organization_owner_client.get(_list_url(organization)).json()

        assert data["count"] == 1
        row = data["results"][0]
        assert row["invitee_display_name"] == (nonmember_user.preferred_name or nonmember_user.get_full_name())
        assert nonmember_user.email not in orjson.dumps(data).decode()

    def test_revoke_removes_invitation_and_blocks_accept(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        token_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]
        url = reverse("api:revoke_member_invitation", kwargs={"slug": organization.slug, "invitation_id": token_id})

        assert organization_owner_client.delete(url).status_code == 204

        assert not OrganizationToken.objects.filter(pk=token_id).exists()
        assert _claim(nonmember_user, token_id).status_code == 400

    def test_invitee_who_became_member_another_way_is_not_pending(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        token_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]
        OrganizationMember.objects.create(organization=organization, user=nonmember_user, tier=tier)

        assert organization_owner_client.get(_list_url(organization)).json()["count"] == 0
        lookup = reverse("api:lookup_organization_user", kwargs={"slug": organization.slug})
        body = organization_owner_client.get(lookup, {"email": nonmember_user.email}).json()
        assert body["pending_invitation_id"] is None
        url = reverse("api:revoke_member_invitation", kwargs={"slug": organization.slug, "invitation_id": token_id})
        assert organization_owner_client.delete(url).status_code == 404

    @pytest.mark.parametrize("state", ["used", "expired"])
    def test_revoke_only_pending_invitations(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
        state: str,
    ) -> None:
        token_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]
        if state == "used":
            OrganizationToken.objects.filter(pk=token_id).update(uses=1)
        else:
            OrganizationToken.objects.filter(pk=token_id).update(expires_at=timezone.now() - dt.timedelta(minutes=1))
        url = reverse("api:revoke_member_invitation", kwargs={"slug": organization.slug, "invitation_id": token_id})

        assert organization_owner_client.delete(url).status_code == 404
        assert OrganizationToken.objects.filter(pk=token_id).exists()

    def test_revoke_rejects_generic_token_of_same_org(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        generic = OrganizationToken.objects.create(
            organization=organization, issuer=organization_owner_user, grants_membership=True, membership_tier=tier
        )
        url = reverse("api:revoke_member_invitation", kwargs={"slug": organization.slug, "invitation_id": generic.pk})

        assert organization_owner_client.delete(url).status_code == 404
        assert OrganizationToken.objects.filter(pk=generic.pk).exists()


class TestPermissions:
    def test_member_without_staff_forbidden(
        self,
        member_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        assert _invite(member_client, organization, nonmember_user, tier).status_code == 403
        assert member_client.get(_list_url(organization)).status_code == 403

    def test_staff_without_manage_members_forbidden(
        self,
        organization_staff_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        assert _invite(organization_staff_client, organization, nonmember_user, tier).status_code == 403

    def test_staff_with_manage_members_allowed(
        self,
        organization_staff_client: Client,
        staff_member: OrganizationStaff,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        staff_member.permissions["default"]["manage_members"] = True
        staff_member.save(update_fields=["permissions"])

        assert _invite(organization_staff_client, organization, nonmember_user, tier).status_code == 201

    def test_anonymous_unauthorized(self, client: Client, organization: Organization) -> None:
        assert client.get(_list_url(organization)).status_code == 401


class TestGenericTokenRoutesIgnoreInvitations:
    def _make(
        self,
        client: Client,
        organization: Organization,
        user: RevelUser,
        tier: MembershipTier,
    ) -> str:
        return str(_invite(client, organization, user, tier).json()["id"])

    def test_list_excludes_personal_invitations(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        token_id = self._make(organization_owner_client, organization, nonmember_user, tier)

        url = reverse("api:list_organization_tokens", kwargs={"slug": organization.slug})
        ids = [row["id"] for row in organization_owner_client.get(url).json()["results"]]

        assert token_id not in ids

    def test_update_and_delete_are_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        token_id = self._make(organization_owner_client, organization, nonmember_user, tier)

        update = reverse("api:edit_organization_token", kwargs={"slug": organization.slug, "token_id": token_id})
        delete = reverse("api:delete_organization_token", kwargs={"slug": organization.slug, "token_id": token_id})

        assert (
            organization_owner_client.put(
                update, data=orjson.dumps({"max_uses": 50}), content_type="application/json"
            ).status_code
            == 404
        )
        assert organization_owner_client.delete(delete).status_code == 404
        assert OrganizationToken.objects.get(pk=token_id).max_uses == 1


class TestRelationshipGates:
    def test_old_direct_add_is_gone(
        self, organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        url = f"/api/organization-admin/{organization.slug}/members/{nonmember_user.pk}"
        response = organization_owner_client.post(url, data=b"{}", content_type="application/json")
        assert response.status_code in (404, 405)
        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_has_relationship_matrix(
        self, organization: Organization, organization_owner_user: RevelUser, nonmember_user: RevelUser
    ) -> None:
        assert organization_service.has_relationship(organization, organization_owner_user)
        assert not organization_service.has_relationship(organization, nonmember_user)
        OrganizationMembershipRequest.objects.create(
            organization=organization,
            user=nonmember_user,
            status=OrganizationMembershipRequest.Status.REJECTED,
        )
        assert organization_service.has_relationship(organization, nonmember_user)

    @pytest.mark.parametrize("status", list(OrganizationMember.MembershipStatus))
    def test_has_relationship_via_member_row_in_any_status(
        self, organization: Organization, nonmember_user: RevelUser, status: OrganizationMember.MembershipStatus
    ) -> None:
        OrganizationMember.objects.create(organization=organization, user=nonmember_user, status=status)
        assert organization_service.has_relationship(organization, nonmember_user)

    def test_has_relationship_via_staff_row(self, organization: Organization, nonmember_user: RevelUser) -> None:
        OrganizationStaff.objects.create(organization=organization, user=nonmember_user)
        assert organization_service.has_relationship(organization, nonmember_user)

    def test_has_relationship_via_register_record(self, organization: Organization, nonmember_user: RevelUser) -> None:
        MembershipRecord.objects.create(
            organization=organization,
            user=nonmember_user,
            full_name="Some Person",
            valid_from=timezone.now() - dt.timedelta(days=1),
            source=MembershipRecord.Source.MANUAL,
        )
        assert organization_service.has_relationship(organization, nonmember_user)

    def test_lookup_reports_pending_invitation(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        tier: MembershipTier,
    ) -> None:
        RevelUser.objects.filter(pk=nonmember_user.pk).update(email_verified=True)
        url = reverse("api:lookup_organization_user", kwargs={"slug": organization.slug})
        assert (
            organization_owner_client.get(url, {"email": nonmember_user.email}).json()["pending_invitation_id"] is None
        )

        token_id = _invite(organization_owner_client, organization, nonmember_user, tier).json()["id"]

        body = organization_owner_client.get(url, {"email": nonmember_user.email}).json()
        assert body["pending_invitation_id"] == token_id
        assert body["is_member"] is False
