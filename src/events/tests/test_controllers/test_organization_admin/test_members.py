"""Tests for organization admin member and membership tier endpoints."""

import uuid

import orjson
import pytest
from django.db.models import Max
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMember, OrganizationStaff

pytestmark = pytest.mark.django_db


class TestManageMembersAndStaff:
    def test_list_members(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        """Test listing organization members."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["user"]["email"] == member_user.email

    def test_list_members_by_staff_without_manage_members(
        self, organization_staff_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        """Any staff can list members even without manage_members (read relaxed to staff)."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})
        response = organization_staff_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["user"]["email"] == member_user.email

    def test_list_members_by_member_forbidden(self, member_client: Client, organization: Organization) -> None:
        """A regular member (non-staff) cannot list members."""
        url = reverse("api:list_organization_members", kwargs={"slug": organization.slug})
        response = member_client.get(url)
        assert response.status_code == 403

    def test_list_staff(
        self, organization_owner_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test listing organization staff."""
        url = reverse("api:list_organization_staff", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["user"]["email"] == staff_member.user.email

    def test_remove_member(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        """Test removing a member from an organization."""
        OrganizationMember.objects.create(organization=organization, user=member_user)
        url = reverse("api:remove_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
        response = organization_owner_client.delete(url)
        assert response.status_code == 204
        assert not OrganizationMember.objects.filter(organization=organization, user=member_user).exists()

    def test_add_member(
        self, organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test adding a new member to an organization."""
        tier = MembershipTier.objects.create(organization=organization, name="Gold")
        url = reverse(
            "api:create_organization_member", kwargs={"slug": organization.slug, "user_id": nonmember_user.id}
        )
        payload = {"tier_id": str(tier.id)}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 201, response.content
        member = OrganizationMember.objects.filter(organization=organization, user=nonmember_user).first()
        assert member is not None
        assert member.tier == tier

    def test_add_member_already_exists(
        self, organization_owner_client: Client, organization: Organization, member_user: RevelUser
    ) -> None:
        """Test that adding an existing member returns an error."""
        tier = MembershipTier.objects.create(organization=organization, name="Silver")
        OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)
        url = reverse("api:create_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
        payload = {"tier_id": str(tier.id)}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 400

    def test_add_member_with_invalid_tier(
        self, organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test that adding a member with an invalid tier returns 404."""
        url = reverse(
            "api:create_organization_member", kwargs={"slug": organization.slug, "user_id": nonmember_user.id}
        )
        payload = {"tier_id": str(uuid.uuid4())}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 404

    def test_add_member_with_tier_from_other_org(
        self,
        organization_owner_client: Client,
        organization: Organization,
        organization_owner_user: RevelUser,
        nonmember_user: RevelUser,
    ) -> None:
        """Test that adding a member with a tier from another organization returns 404."""
        other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
        other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")
        url = reverse(
            "api:create_organization_member", kwargs={"slug": organization.slug, "user_id": nonmember_user.id}
        )
        payload = {"tier_id": str(other_tier.id)}
        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 404

    def test_add_staff(
        self, organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test adding a new staff member."""
        url = reverse("api:create_organization_staff", kwargs={"slug": organization.slug, "user_id": nonmember_user.id})
        response = organization_owner_client.post(url, content_type="application/json")
        assert response.status_code == 201, response.content
        assert OrganizationStaff.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_remove_staff(
        self, organization_owner_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test removing a staff member."""
        url = reverse(
            "api:remove_organization_staff", kwargs={"slug": organization.slug, "user_id": staff_member.user.id}
        )
        response = organization_owner_client.delete(url)
        assert response.status_code == 204
        assert not OrganizationStaff.objects.filter(organization=organization, user=staff_member.user).exists()

    def test_remove_staff_by_staff_with_manage_members_fails(
        self,
        organization_staff_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
    ) -> None:
        """Test that a staff member with manage_members cannot remove staff (owner-only)."""
        # Grant manage_members to the requesting staff
        perms = staff_member.permissions
        perms["default"]["manage_members"] = True
        staff_member.permissions = perms
        staff_member.save()

        # Create another staff member to attempt removal on
        other_staff_user = RevelUser.objects.create_user("otherstaff_remove@example.com")
        OrganizationStaff.objects.create(organization=organization, user=other_staff_user)

        url = reverse(
            "api:remove_organization_staff", kwargs={"slug": organization.slug, "user_id": other_staff_user.id}
        )
        response = organization_staff_client.delete(url)
        assert response.status_code == 403
        # Verify staff was NOT removed
        assert OrganizationStaff.objects.filter(organization=organization, user=other_staff_user).exists()

    def test_remove_staff_by_nonmember_fails(
        self,
        nonmember_client: Client,
        organization: Organization,
        staff_member: OrganizationStaff,
    ) -> None:
        """Test that a nonmember gets 404 (org not visible via scoped queryset)."""
        url = reverse(
            "api:remove_organization_staff", kwargs={"slug": organization.slug, "user_id": staff_member.user.id}
        )
        response = nonmember_client.delete(url)
        assert response.status_code == 404
        # Verify staff was NOT removed
        assert OrganizationStaff.objects.filter(organization=organization, user=staff_member.user).exists()

    def test_update_staff_permissions_by_owner(
        self, organization_owner_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that the owner can update staff permissions."""
        url = reverse(
            "api:update_staff_permissions", kwargs={"slug": organization.slug, "user_id": staff_member.user.id}
        )
        payload = {"default": {"manage_members": True, "create_event": True}, "event_overrides": {}}
        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 200
        staff_member.refresh_from_db()
        assert staff_member.permissions["default"]["manage_members"] is True
        assert staff_member.permissions["default"]["create_event"] is True

    def test_update_staff_permissions_by_staff_fails(
        self, organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
    ) -> None:
        """Test that a staff member cannot update another staff member's permissions."""
        # Create another staff member for the test
        other_staff_user = RevelUser.objects.create_user("otherstaff@example.com")
        OrganizationStaff.objects.create(organization=organization, user=other_staff_user)

        url = reverse(
            "api:update_staff_permissions", kwargs={"slug": organization.slug, "user_id": other_staff_user.id}
        )
        payload = {"default": {"manage_members": True}}
        response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")
        assert response.status_code == 403


# ---- Membership Tier Tests ----


def test_list_membership_tiers_by_staff(organization_staff_client: Client, organization: Organization) -> None:
    """Test that staff can list membership tiers."""
    # Create some tiers (note: organization signal already creates "General membership")
    MembershipTier.objects.create(organization=organization, name="Gold")
    MembershipTier.objects.create(organization=organization, name="Silver")

    url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
    response = organization_staff_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3  # General membership + Gold + Silver
    tier_names = {tier["name"] for tier in data}
    assert tier_names == {"General membership", "Gold", "Silver"}


def test_list_membership_tiers_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can list membership tiers."""
    MembershipTier.objects.create(organization=organization, name="Premium")

    url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
    response = organization_owner_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2  # General membership + Premium
    tier_names = {tier["name"] for tier in data}
    assert "Premium" in tier_names
    assert "General membership" in tier_names


def test_list_membership_tiers_by_member_forbidden(member_client: Client, organization: Organization) -> None:
    """Test that regular members cannot list membership tiers."""
    url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
    response = member_client.get(url)

    assert response.status_code == 403


def test_create_membership_tier_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can create a membership tier."""
    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "VIP"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "VIP"
    assert "id" in data

    # Verify it was created in DB
    assert MembershipTier.objects.filter(organization=organization, name="VIP").exists()


def test_create_membership_tier_appends_at_bottom(
    organization_owner_client: Client, organization: Organization
) -> None:
    """New tiers get the next display_order so they append at the bottom, not jump to the top (#514)."""
    existing_max = MembershipTier.objects.filter(organization=organization).aggregate(m=Max("display_order"))["m"] or 0
    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})

    response = organization_owner_client.post(
        url, data=orjson.dumps({"name": "Appended"}), content_type="application/json"
    )

    assert response.status_code == 201
    tier = MembershipTier.objects.get(organization=organization, name="Appended")
    assert tier.display_order == existing_max + 1
    # It sorts last under the model's default ordering.
    assert MembershipTier.objects.filter(organization=organization).last() == tier


def test_create_membership_tier_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff with manage_members permission can create tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "Bronze"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Bronze"


def test_create_membership_tier_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff without manage_members permission cannot create tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = False
    staff_member.permissions = perms
    staff_member.save()

    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "Platinum"}

    response = organization_staff_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_create_duplicate_membership_tier_name_fails(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Test that creating a tier with duplicate name in same org fails."""
    MembershipTier.objects.create(organization=organization, name="Gold")

    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "Gold"}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_create_membership_tier_with_empty_name_fails(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Test that creating a tier with empty/whitespace-only name fails."""
    url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
    payload = {"name": "   "}

    response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 422


def test_update_membership_tier_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can update a membership tier."""
    tier = MembershipTier.objects.create(organization=organization, name="Old Name")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    payload = {"name": "New Name"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Name"

    tier.refresh_from_db()
    assert tier.name == "New Name"


def test_update_membership_tier_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff with manage_members permission can update tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    tier = MembershipTier.objects.create(organization=organization, name="Original")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    payload = {"name": "Updated"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    tier.refresh_from_db()
    assert tier.name == "Updated"


def test_update_membership_tier_to_duplicate_name_fails(
    organization_owner_client: Client, organization: Organization
) -> None:
    """Test that updating a tier to a duplicate name fails."""
    MembershipTier.objects.create(organization=organization, name="Gold")
    tier2 = MembershipTier.objects.create(organization=organization, name="Silver")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier2.id})
    payload = {"name": "Gold"}  # Duplicate name

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 400


def test_update_membership_tier_of_another_organization_fails(
    organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that updating a tier from another organization fails with 404."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": other_tier.id})
    payload = {"name": "Hacked Name"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_delete_membership_tier_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that owner can delete a membership tier."""
    tier = MembershipTier.objects.create(organization=organization, name="To Delete")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not MembershipTier.objects.filter(id=tier.id).exists()


def test_delete_membership_tier_sets_members_tier_to_null(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that deleting a tier sets members' tier FK to NULL."""
    tier = MembershipTier.objects.create(organization=organization, name="To Delete")
    member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    member.refresh_from_db()
    assert member.tier is None


def test_delete_membership_tier_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff with manage_members permission can delete tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    tier = MembershipTier.objects.create(organization=organization, name="Deletable")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_staff_client.delete(url)

    assert response.status_code == 204
    assert not MembershipTier.objects.filter(id=tier.id).exists()


def test_delete_membership_tier_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff without manage_members permission cannot delete tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = False
    staff_member.permissions = perms
    staff_member.save()

    tier = MembershipTier.objects.create(organization=organization, name="Protected")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": tier.id})
    response = organization_staff_client.delete(url)

    assert response.status_code == 403


def test_delete_membership_tier_of_another_organization_fails(
    organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that deleting a tier from another organization fails with 404."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    url = reverse("api:delete_membership_tier", kwargs={"slug": organization.slug, "tier_id": other_tier.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404
    # Ensure it wasn't deleted
    assert MembershipTier.objects.filter(id=other_tier.id).exists()


# ---- Membership Tier Reorder Tests ----


def test_membership_tier_list_exposes_display_order(
    organization_staff_client: Client, organization: Organization
) -> None:
    """The tier list serializer exposes display_order so the FE can render/persist ordering."""
    MembershipTier.objects.create(organization=organization, name="Alpha")
    MembershipTier.objects.create(organization=organization, name="Beta")

    url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
    response = organization_staff_client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert all("display_order" in tier for tier in data)


def test_reorder_membership_tiers_by_owner(organization_owner_client: Client, organization: Organization) -> None:
    """Test that an organization owner can reorder membership tiers successfully.

    A default tier is created by a signal, so the submitted list must cover all tiers.
    """
    MembershipTier.objects.create(organization=organization, name="Gold")
    MembershipTier.objects.create(organization=organization, name="Silver")
    # Submit all tier IDs in reverse of their current (name) ordering.
    current = list(MembershipTier.objects.filter(organization=organization).values_list("id", flat=True))
    desired_order = [str(pk) for pk in reversed(current)]
    url = reverse("api:reorder_membership_tiers", kwargs={"slug": organization.slug})

    response = organization_owner_client.patch(
        url, data=orjson.dumps({"tier_ids": desired_order}), content_type="application/json"
    )

    assert response.status_code == 204
    reordered = list(MembershipTier.objects.filter(organization=organization).order_by("display_order"))
    assert [str(tier.pk) for tier in reordered] == desired_order
    assert [tier.display_order for tier in reordered] == list(range(len(desired_order)))


def test_reorder_membership_tiers_by_staff_with_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff with manage_members permission can reorder tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    MembershipTier.objects.create(organization=organization, name="Gold")
    MembershipTier.objects.create(organization=organization, name="Silver")
    current = list(MembershipTier.objects.filter(organization=organization).values_list("id", flat=True))
    desired_order = [str(pk) for pk in reversed(current)]
    url = reverse("api:reorder_membership_tiers", kwargs={"slug": organization.slug})

    response = organization_staff_client.patch(
        url, data=orjson.dumps({"tier_ids": desired_order}), content_type="application/json"
    )

    assert response.status_code == 204
    reordered = list(MembershipTier.objects.filter(organization=organization).order_by("display_order"))
    assert [str(tier.pk) for tier in reordered] == desired_order


def test_reorder_membership_tiers_by_staff_without_permission(
    organization_staff_client: Client, organization: Organization, staff_member: OrganizationStaff
) -> None:
    """Test that staff without manage_members permission cannot reorder tiers."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = False
    staff_member.permissions = perms
    staff_member.save()

    gold = MembershipTier.objects.create(organization=organization, name="Gold")
    silver = MembershipTier.objects.create(organization=organization, name="Silver")
    desired_order = [str(silver.pk), str(gold.pk)]
    url = reverse("api:reorder_membership_tiers", kwargs={"slug": organization.slug})

    response = organization_staff_client.patch(
        url, data=orjson.dumps({"tier_ids": desired_order}), content_type="application/json"
    )

    assert response.status_code == 403


def test_reorder_membership_tiers_subset_fails(organization_owner_client: Client, organization: Organization) -> None:
    """Test that submitting a subset of the org's tier IDs returns 400."""
    gold = MembershipTier.objects.create(organization=organization, name="Gold")
    MembershipTier.objects.create(organization=organization, name="Silver")
    url = reverse("api:reorder_membership_tiers", kwargs={"slug": organization.slug})

    response = organization_owner_client.patch(
        url, data=orjson.dumps({"tier_ids": [str(gold.pk)]}), content_type="application/json"
    )

    assert response.status_code == 400


def test_reorder_membership_tiers_foreign_tier_fails(
    organization_owner_client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that including a tier ID from another organization returns 400."""
    gold = MembershipTier.objects.create(organization=organization, name="Gold")
    silver = MembershipTier.objects.create(organization=organization, name="Silver")
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    foreign = MembershipTier.objects.create(organization=other_org, name="Foreign")
    desired_order = [str(silver.pk), str(gold.pk), str(foreign.pk)]
    url = reverse("api:reorder_membership_tiers", kwargs={"slug": organization.slug})

    response = organization_owner_client.patch(
        url, data=orjson.dumps({"tier_ids": desired_order}), content_type="application/json"
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    "client_fixture,expected_status_code", [("member_client", 403), ("nonmember_client", 404), ("client", 401)]
)
def test_reorder_membership_tiers_unauthorized(
    request: pytest.FixtureRequest,
    client_fixture: str,
    expected_status_code: int,
    organization: Organization,
) -> None:
    """Test that unauthorized users cannot reorder membership tiers."""
    client: Client = request.getfixturevalue(client_fixture)
    gold = MembershipTier.objects.create(organization=organization, name="Gold")
    silver = MembershipTier.objects.create(organization=organization, name="Silver")
    desired_order = [str(silver.pk), str(gold.pk)]
    url = reverse("api:reorder_membership_tiers", kwargs={"slug": organization.slug})

    response = client.patch(url, data=orjson.dumps({"tier_ids": desired_order}), content_type="application/json")

    assert response.status_code == expected_status_code


# ---- Organization Member Update Tests ----


def test_update_member_status_by_owner(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can update member status."""
    member = OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "paused"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "paused"

    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.PAUSED


def test_update_member_tier_by_owner(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can update member tier."""
    tier = MembershipTier.objects.create(organization=organization, name="Premium")
    member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=None)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"tier_id": str(tier.id)}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["tier"]["id"] == str(tier.id)
    assert data["tier"]["name"] == "Premium"

    member.refresh_from_db()
    assert member.tier == tier


def test_update_member_both_status_and_tier(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can update both status and tier simultaneously."""
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    member = OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE, tier=None
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "cancelled", "tier_id": str(tier.id)}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "cancelled"
    assert data["tier"]["name"] == "Gold"

    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.CANCELLED
    assert member.tier == tier


def test_update_member_remove_tier(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that owner can remove tier assignment by setting tier_id to null."""
    tier = MembershipTier.objects.create(organization=organization, name="Basic")
    member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=tier)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"tier_id": None}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["tier"] is None

    member.refresh_from_db()
    assert member.tier is None


def test_update_member_by_staff_with_permission(
    organization_staff_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    member_user: RevelUser,
) -> None:
    """Test that staff with manage_members permission can update members."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = True
    staff_member.permissions = perms
    staff_member.save()

    member = OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "banned"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 200
    member.refresh_from_db()
    assert member.status == OrganizationMember.MembershipStatus.BANNED


def test_update_member_by_staff_without_permission(
    organization_staff_client: Client,
    organization: Organization,
    staff_member: OrganizationStaff,
    member_user: RevelUser,
) -> None:
    """Test that staff without manage_members permission cannot update members."""
    perms = staff_member.permissions
    perms["default"]["manage_members"] = False
    staff_member.permissions = perms
    staff_member.save()

    OrganizationMember.objects.create(
        organization=organization, user=member_user, status=OrganizationMember.MembershipStatus.ACTIVE
    )

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "paused"}

    response = organization_staff_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 403


def test_update_member_with_tier_from_another_org_fails(
    organization_owner_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
    member_user: RevelUser,
) -> None:
    """Test that assigning a tier from another organization fails with 404."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    member = OrganizationMember.objects.create(organization=organization, user=member_user)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"tier_id": str(other_tier.id)}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404
    member.refresh_from_db()
    assert member.tier is None


def test_update_member_nonexistent_member_fails(
    organization_owner_client: Client, organization: Organization, nonmember_user: RevelUser
) -> None:
    """Test that updating a non-member fails with 404."""
    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": nonmember_user.id})
    payload = {"status": "paused"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 404


def test_update_member_invalid_status_fails(
    organization_owner_client: Client, organization: Organization, member_user: RevelUser
) -> None:
    """Test that updating with invalid status value fails."""
    OrganizationMember.objects.create(organization=organization, user=member_user)

    url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
    payload = {"status": "invalid_status"}

    response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

    assert response.status_code == 422
