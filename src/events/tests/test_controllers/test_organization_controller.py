import typing as t
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.client import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    OrganizationToken,
)
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


# --- Tests for GET /organizations/ ---


def test_list_organizations_visibility(
    client: Client,
    nonmember_client: Client,
    member_client: Client,
    organization_staff_client: Client,
    organization_owner_client: Client,
    superuser_client: Client,
    organization: Organization,
    organization_owner_user: RevelUser,
) -> None:
    """Test that the organization list respects user visibility rules."""
    # `organization` is private by default and is linked to all relevant clients.
    public_org = Organization.objects.create(
        name="Public Org", slug="public-org", owner=organization_owner_user, visibility=Organization.Visibility.PUBLIC
    )
    url = reverse("api:list_organizations")

    # Anonymous and non-member clients should only see the public organization.
    for c in [client, nonmember_client]:
        response = c.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["name"] == public_org.name

    # Member, staff, and owner should see the public org and their own private org.
    for c in [member_client, organization_staff_client, organization_owner_client]:
        response = c.get(url)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        names = {org["name"] for org in data["results"]}
        assert {organization.name, public_org.name} == names

    # Superuser sees all organizations.
    response = superuser_client.get(url)
    assert response.status_code == 200
    assert response.json()["count"] == 2


def test_list_organizations_search(client: Client, organization_owner_user: RevelUser) -> None:
    """Test searching for organizations by name and description."""
    Organization.objects.create(
        name="Tech Conference",
        slug="tech",
        owner=organization_owner_user,
        visibility="public",
        description="A conference about technology.",
    )
    Organization.objects.create(
        name="Art Fair",
        slug="art",
        owner=organization_owner_user,
        visibility="public",
        description="A fair for artists.",
    )
    url = reverse("api:list_organizations")

    # Search by name
    response = client.get(url, {"search": "Tech"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Tech Conference"

    # Search by description
    response = client.get(url, {"search": "artists"})
    assert response.status_code == 200
    data = response.json()["results"]
    assert len(data) == 1
    assert data[0]["name"] == "Art Fair"

    # No results
    response = client.get(url, {"search": "nonexistent"})
    assert response.status_code == 200
    assert len(response.json()["results"]) == 0


# --- Tests for GET /organizations/{slug}/ ---


def test_get_organization_visibility(
    client: Client, nonmember_client: Client, member_client: Client, organization: Organization
) -> None:
    """Test retrieving a single organization based on visibility rules."""
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    # Initially private, anonymous/non-member can't see it, but member can.
    organization.visibility = "private"
    organization.save()
    assert client.get(url).status_code == 404
    assert nonmember_client.get(url).status_code == 404
    assert member_client.get(url).status_code == 200

    # When public, everyone can see it.
    organization.visibility = "public"
    organization.save()
    assert client.get(url).status_code == 200
    assert nonmember_client.get(url).status_code == 200
    assert member_client.get(url).status_code == 200


def test_get_organization_by_privileged_users(
    organization_owner_client: Client, organization_staff_client: Client, organization: Organization
) -> None:
    """Test that owner and staff can retrieve a private organization."""
    organization.visibility = "private"
    organization.save()
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    # Owner can see it.
    response = organization_owner_client.get(url)
    assert response.status_code == 200
    assert response.json()["name"] == organization.name

    # Staff can see it.
    response = organization_staff_client.get(url)
    assert response.status_code == 200
    assert response.json()["name"] == organization.name


def test_get_organization_exposes_membership_billing_policy(client: Client, organization: Organization) -> None:
    """The public org payload carries the billing-disclosure numbers (issue #809).

    The subscribe flow tells a prospective member how long the grace period is and how
    long they can rejoin at the old price, so both day counts must be readable anonymously.
    """
    organization.visibility = Organization.Visibility.PUBLIC
    organization.membership_grace_period_days = 14
    organization.membership_subscription_revival_window_days = 45
    organization.membership_refund_policy = "Full refund within 7 days."
    organization.save(
        update_fields=[
            "visibility",
            "membership_grace_period_days",
            "membership_subscription_revival_window_days",
            "membership_refund_policy",
        ]
    )
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    response = client.get(url)

    assert response.status_code == 200
    data = response.json()
    assert data["membership_grace_period_days"] == 14
    assert data["membership_subscription_revival_window_days"] == 45
    assert data["membership_refund_policy"] == "Full refund within 7 days."


class TestListMembershipPlans:
    """Public listing of an organization's active subscription plans.

    Visibility piggybacks on ``get_one`` so private orgs return 404 for
    anonymous users (same rule as ``get_organization``).
    """

    def test_lists_active_plans_only(self, client: Client, organization: Organization) -> None:
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save(update_fields=["visibility"])
        tier = MembershipTier.objects.get(organization=organization, name="General membership")
        active = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
        )
        MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Old Annual",
            price=Decimal("100.00"),
            currency="EUR",
            period_unit="year",
            is_active=False,
        )

        url = reverse("api:list_organization_membership_plans", kwargs={"slug": organization.slug})
        response = client.get(url)
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["id"] == str(active.id)
        assert body[0]["payment_method"] == "offline"
        # The frontend groups public plan cards by tier and needs the label, not just the id.
        assert body[0]["tier_id"] == str(tier.id)
        assert body[0]["tier_name"] == tier.name

    def test_anonymous_can_list_public_org_plans(self, client: Client, organization: Organization) -> None:
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save(update_fields=["visibility"])
        url = reverse("api:list_organization_membership_plans", kwargs={"slug": organization.slug})
        response = client.get(url)
        assert response.status_code == 200

    def test_anonymous_blocked_on_private_org(self, client: Client, organization: Organization) -> None:
        # Default visibility is private; the endpoint inherits get_one's visibility rule.
        url = reverse("api:list_organization_membership_plans", kwargs={"slug": organization.slug})
        response = client.get(url)
        assert response.status_code == 404


class TestListMembershipTiers:
    """Public listing of an organization's membership tiers (issue #830).

    Without it a tier carrying no subscription plan is invisible to prospective
    members, so gated free tiers are unreachable. Visibility piggybacks on
    ``get_one``, so the rules match ``get_organization``.
    """

    @staticmethod
    def _url(slug: str) -> str:
        return reverse("api:list_organization_membership_tiers", kwargs={"slug": slug})

    @staticmethod
    def _publish(organization: Organization) -> None:
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save(update_fields=["visibility"])

    @staticmethod
    def _membership_oq(organization: Organization, name: str) -> OrganizationQuestionnaire:
        """Wrap a published questionnaire as a MEMBERSHIP OrganizationQuestionnaire."""
        questionnaire = Questionnaire.objects.create(name=name, status=Questionnaire.QuestionnaireStatus.PUBLISHED)
        return OrganizationQuestionnaire.objects.create(
            organization=organization,
            questionnaire=questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        )

    def test_anonymous_sees_tiers_in_display_order(self, client: Client, organization: Organization) -> None:
        """Ordering follows ``display_order``, not the tier name (deliberately unlike the plans route)."""
        self._publish(organization)
        default_tier = MembershipTier.objects.get(organization=organization, name="General membership")
        default_tier.display_order = 5
        default_tier.save(update_fields=["display_order"])
        # Names chosen so alphabetical order would be Alpha, Zeta, General membership.
        zeta = MembershipTier.objects.create(organization=organization, name="Zeta", display_order=0)
        alpha = MembershipTier.objects.create(organization=organization, name="Alpha", display_order=2)

        response = client.get(self._url(organization.slug))

        assert response.status_code == 200
        body = response.json()
        assert [row["id"] for row in body] == [str(zeta.id), str(alpha.id), str(default_tier.id)]
        assert [row["display_order"] for row in body] == [0, 2, 5]

    def test_same_list_for_member_and_nonmember(
        self, client: Client, member_client: Client, nonmember_client: Client, organization: Organization
    ) -> None:
        """The listing is visibility-aware, not membership-aware — everyone sees the same tiers."""
        self._publish(organization)
        MembershipTier.objects.create(organization=organization, name="Supporter", display_order=1)

        payloads = []
        for c in (client, member_client, nonmember_client):
            response = c.get(self._url(organization.slug))
            assert response.status_code == 200
            payloads.append(response.json())

        assert payloads[0] == payloads[1] == payloads[2]
        assert len(payloads[0]) == 2

    def test_private_org_is_404_for_anonymous_and_authenticated(
        self, client: Client, nonmember_client: Client, organization: Organization
    ) -> None:
        # `organization` is private by default.
        assert client.get(self._url(organization.slug)).status_code == 404
        assert nonmember_client.get(self._url(organization.slug)).status_code == 404

    def test_nonexistent_slug_is_404(self, client: Client) -> None:
        assert client.get(self._url("no-such-org")).status_code == 404

    def test_free_tier_has_no_plans(self, client: Client, organization: Organization) -> None:
        self._publish(organization)

        response = client.get(self._url(organization.slug))

        assert response.status_code == 200
        (tier,) = response.json()
        assert tier["is_free"] is True
        assert tier["plans"] == []
        assert tier["requires_approval"] is False
        assert tier["questionnaire_id"] is None

    def test_paid_tier_nests_active_plans_only(self, client: Client, organization: Organization) -> None:
        self._publish(organization)
        tier = MembershipTier.objects.get(organization=organization, name="General membership")
        active = MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Monthly",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            max_subscriptions=5,
        )
        MembershipSubscriptionPlan.objects.create(
            tier=tier,
            name="Old Annual",
            price=Decimal("100.00"),
            currency="EUR",
            period_unit="year",
            is_active=False,
        )

        response = client.get(self._url(organization.slug))

        assert response.status_code == 200
        (row,) = response.json()
        assert row["is_free"] is False
        assert len(row["plans"]) == 1
        plan = row["plans"][0]
        assert plan["id"] == str(active.id)
        assert plan["name"] == "Monthly"
        assert plan["price"] == "10.00"
        assert plan["currency"] == "EUR"
        assert plan["period_unit"] == "month"
        assert plan["payment_method"] == "offline"
        assert plan["sales_status"] == "open"
        assert plan["sold_out"] is False
        assert plan["tier_id"] == str(tier.id)
        assert plan["tier_name"] == tier.name
        # Staff-only occupancy telemetry must not leak through the public surface.
        assert "max_subscriptions" not in plan
        assert "active_subscription_count" not in plan
        assert "stripe_price_id" not in plan

    def test_tier_questionnaire_override_returns_underlying_questionnaire_pk(
        self, client: Client, organization: Organization
    ) -> None:
        """The exposed id is the Questionnaire pk, not the OrganizationQuestionnaire wrapper's."""
        self._publish(organization)
        org_default = self._membership_oq(organization, "Org default")
        organization.default_membership_questionnaire = org_default
        organization.save(update_fields=["default_membership_questionnaire"])
        tier_oq = self._membership_oq(organization, "Tier override")
        tier = MembershipTier.objects.get(organization=organization, name="General membership")
        tier.membership_questionnaire = tier_oq
        tier.save(update_fields=["membership_questionnaire"])

        response = client.get(self._url(organization.slug))

        assert response.status_code == 200
        (row,) = response.json()
        assert row["questionnaire_id"] == str(tier_oq.questionnaire_id)
        assert row["questionnaire_id"] != str(tier_oq.id)

    def test_org_default_questionnaire_is_inherited(self, client: Client, organization: Organization) -> None:
        self._publish(organization)
        org_default = self._membership_oq(organization, "Org default")
        organization.default_membership_questionnaire = org_default
        organization.save(update_fields=["default_membership_questionnaire"])
        tier = MembershipTier.objects.get(organization=organization, name="General membership")
        assert tier.membership_questionnaire_id is None

        response = client.get(self._url(organization.slug))

        assert response.status_code == 200
        (row,) = response.json()
        assert row["questionnaire_id"] == str(org_default.questionnaire_id)

    def test_requires_approval_resolution(self, client: Client, organization: Organization) -> None:
        """Tier override wins when set; NULL inherits the org default."""
        self._publish(organization)
        organization.default_requires_membership_approval = True
        organization.save(update_fields=["default_requires_membership_approval"])
        inherits = MembershipTier.objects.get(organization=organization, name="General membership")
        overrides = MembershipTier.objects.create(
            organization=organization,
            name="Open tier",
            display_order=1,
            requires_membership_approval=False,
        )
        assert inherits.requires_membership_approval is None

        response = client.get(self._url(organization.slug))

        assert response.status_code == 200
        by_id = {row["id"]: row["requires_approval"] for row in response.json()}
        assert by_id[str(inherits.id)] is True
        assert by_id[str(overrides.id)] is False

    def test_query_count_is_constant(
        self, client: Client, organization: Organization, django_assert_num_queries: t.Any
    ) -> None:
        """Tiers and their plans are prefetched — more tiers must not add queries."""
        self._publish(organization)
        url = self._url(organization.slug)

        def add_tier(name: str, order: int) -> None:
            tier = MembershipTier.objects.create(organization=organization, name=name, display_order=order)
            MembershipSubscriptionPlan.objects.create(
                tier=tier, name=f"{name} monthly", price=Decimal("9.00"), currency="EUR"
            )

        add_tier("First", 1)
        with CaptureQueriesContext(connection) as captured:
            assert client.get(url).status_code == 200
        baseline = len(captured.captured_queries)

        for i in range(3):
            add_tier(f"Extra {i}", 2 + i)

        with django_assert_num_queries(baseline):
            response = client.get(url)
        assert len(response.json()) == 5


def test_get_organization_not_found(client: Client) -> None:
    """Test that a 404 is returned for a non-existent organization slug."""
    url = reverse("api:get_organization", kwargs={"slug": "non-existent-slug"})
    response = client.get(url)
    assert response.status_code == 404


class TestClaimInvitation:
    def test_claim_invitation_success(
        self, nonmember_client: Client, organization_token: OrganizationToken, nonmember_user: RevelUser
    ) -> None:
        """Test that an invitation is claimed successfully."""
        url = reverse("api:organization_claim_invitation", kwargs={"token": organization_token.id})
        response = nonmember_client.post(url)
        assert response.status_code == 200
        assert OrganizationMember.objects.filter(
            organization=organization_token.organization, user=nonmember_user
        ).exists()

    def test_claim_invitation_unauthorized(self, client: Client, organization_token: OrganizationToken) -> None:
        """Test that an unauthenticated user cannot claim an invitation."""
        url = reverse("api:organization_claim_invitation", kwargs={"token": organization_token.id})
        response = client.post(url)
        assert response.status_code == 401

    def test_claim_invitation_invalid_token(self, nonmember_client: Client) -> None:
        """Test that an invalid token returns a 400."""
        url = reverse("api:organization_claim_invitation", kwargs={"token": "invalid-token"})
        response = nonmember_client.post(url)
        assert response.status_code == 400


class TestCreateMembershipRequest:
    def test_create_membership_request_success(
        self, nonmember_client: Client, organization: Organization, nonmember_user: RevelUser
    ) -> None:
        """Test that a membership request is created successfully."""
        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()
        url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
        response = nonmember_client.post(url, content_type="application/json")
        assert response.status_code == 200, response.json()
        assert OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_create_membership_request_unauthorized(self, client: Client, organization: Organization) -> None:
        """Test that an unauthenticated user cannot create a membership request."""
        url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
        response = client.post(url)
        assert response.status_code == 401

    def test_create_membership_request_already_member(self, member_client: Client, organization: Organization) -> None:
        """Test that a member cannot create a membership request."""
        url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
        response = member_client.post(url)
        assert response.status_code == 400

    def test_create_membership_request_blacklisted_user_rejected(
        self,
        nonmember_client: Client,
        nonmember_user: RevelUser,
        organization: Organization,
        organization_owner_user: RevelUser,
    ) -> None:
        """Test that a blacklisted user cannot create a membership request.

        The for_user() queryset excludes blacklisted orgs, so the user gets 404
        (org not visible). The service-layer blacklist check provides defense-in-depth
        and is tested separately in test_organization_service.py.
        """
        from events.models import Blacklist

        organization.visibility = Organization.Visibility.PUBLIC
        organization.save()

        # Blacklist the nonmember user
        Blacklist.objects.create(
            organization=organization,
            user=nonmember_user,
            email=nonmember_user.email,
            created_by=organization_owner_user,
            reason="Banned",
        )

        url = reverse("api:create_membership_request", kwargs={"slug": organization.slug})
        response = nonmember_client.post(url, content_type="application/json")
        assert response.status_code == 404
        assert not OrganizationMembershipRequest.objects.filter(organization=organization, user=nonmember_user).exists()


class TestCreateOrganization:
    """Tests for POST /organizations/ endpoint."""

    def test_create_organization_success_with_verified_email(
        self, nonmember_client: Client, nonmember_user: RevelUser
    ) -> None:
        """Test that a user with verified email can create an organization."""
        # Arrange
        nonmember_user.email_verified = True
        nonmember_user.save()

        url = reverse("api:create_organization")
        payload = {
            "name": "New Acme Collective",
            "description": "A test organization description",
            "contact_email": "contact@neworg.com",
        }

        # Act
        response = nonmember_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New Acme Collective"
        assert data["description"] == "A test organization description"
        # Public schema hides contact_email when contact_method=NONE (the create default).
        assert data["contact_method"] == Organization.ContactMethod.NONE
        assert data["contact_email"] is None
        assert data["visibility"] == Organization.Visibility.STAFF_ONLY
        # Verify the email is actually stored on the model.
        org = Organization.objects.get(name="New Acme Collective", owner=nonmember_user)
        assert org.contact_email == "contact@neworg.com"
        assert org.contact_email_verified is False

    def test_create_organization_with_owner_email_auto_verifies(
        self, nonmember_client: Client, nonmember_user: RevelUser
    ) -> None:
        """Test that contact email is auto-verified when it matches owner's verified email."""
        # Arrange
        nonmember_user.email_verified = True
        nonmember_user.email = "owner@example.com"
        nonmember_user.save()

        url = reverse("api:create_organization")
        payload = {
            "name": "Auto Verify Org",
            "contact_email": "owner@example.com",  # Same as owner's email
        }

        # Act
        response = nonmember_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 201
        data = response.json()
        # Public schema hides contact_email when contact_method=NONE (the create default).
        assert data["contact_method"] == Organization.ContactMethod.NONE
        assert data["contact_email"] is None
        org = Organization.objects.get(name="Auto Verify Org", owner=nonmember_user)
        assert org.contact_email == "owner@example.com"
        assert org.contact_email_verified is True

    def test_create_organization_ignores_membership_policy_fields(
        self, nonmember_client: Client, nonmember_user: RevelUser
    ) -> None:
        """The day counts are readable on the public schema but writable only by admins (issue #809)."""
        # Arrange
        nonmember_user.email_verified = True
        nonmember_user.save()

        url = reverse("api:create_organization")
        payload = {
            "name": "Policy Smuggler",
            "contact_email": "contact@smuggler.com",
            "membership_grace_period_days": 999,
            "membership_subscription_revival_window_days": 999,
        }

        # Act
        response = nonmember_client.post(url, data=payload, content_type="application/json")

        # Assert -- the create schema drops the unknown keys, so the model defaults survive.
        assert response.status_code == 201
        data = response.json()
        assert data["membership_grace_period_days"] == 7
        assert data["membership_subscription_revival_window_days"] == 30
        org = Organization.objects.get(name="Policy Smuggler", owner=nonmember_user)
        assert org.membership_grace_period_days == 7
        assert org.membership_subscription_revival_window_days == 30

    def test_create_organization_without_verified_email_fails(
        self, nonmember_client: Client, nonmember_user: RevelUser
    ) -> None:
        """Test that a user without verified email cannot create an organization."""
        # Arrange
        nonmember_user.email_verified = False
        nonmember_user.save()

        url = reverse("api:create_organization")
        payload = {"name": "Should Fail Org", "contact_email": "contact@fail.com"}

        # Act
        response = nonmember_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 403
        assert "Email verification required" in response.json().get("detail", "")

    def test_create_organization_user_already_owns_one_fails(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Test that a user cannot create a second organization."""
        url = reverse("api:create_organization")
        payload = {"name": "Pebble Society", "contact_email": "second@org.com"}

        # Act
        response = organization_owner_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 400
        assert "already own an organization" in response.json().get("detail", "")

    def test_create_organization_unauthenticated_fails(self, client: Client) -> None:
        """Test that an unauthenticated user cannot create an organization."""
        url = reverse("api:create_organization")
        payload = {"name": "Unauth Org", "contact_email": "unauth@org.com"}

        response = client.post(url, data=payload, content_type="application/json")
        assert response.status_code == 401

    def test_create_organization_invalid_email_fails(self, nonmember_client: Client, nonmember_user: RevelUser) -> None:
        """Test that invalid email format is rejected."""
        # Arrange
        nonmember_user.email_verified = True
        nonmember_user.save()

        url = reverse("api:create_organization")
        payload = {"name": "Bad Email Org", "contact_email": "not-an-email"}

        # Act
        response = nonmember_client.post(url, data=payload, content_type="application/json")

        # Assert
        assert response.status_code == 422  # Validation error


# --- Tests for 410 Gone on expired / used-up organization tokens ---


def test_expired_org_token_returns_410_for_private_org(
    client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """GET /organizations/{slug} with an expired org token returns 410 Gone.

    Previously this returned 404 (indistinguishable from 'org does not exist').
    The 410 lets the frontend show a meaningful message to the user.
    """
    # Arrange
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save()
    token = OrganizationToken.objects.create(
        organization=organization,
        issuer=organization_owner_user,
        grants_membership=False,
        expires_at=timezone.now() - timedelta(hours=1),
    )
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    # Act
    response = client.get(url, HTTP_X_ORG_TOKEN=token.pk)

    # Assert
    assert response.status_code == 410
    assert "expired" in response.json()["detail"].lower()


def test_used_up_org_token_returns_410_for_private_org(
    client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """GET /organizations/{slug} with a fully-used org token returns 410 Gone.

    The response message should mention that the link has reached its maximum
    number of uses.
    """
    # Arrange
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save()
    token = OrganizationToken.objects.create(
        organization=organization,
        issuer=organization_owner_user,
        grants_membership=False,
        expires_at=timezone.now() + timedelta(hours=1),
        max_uses=3,
        uses=3,
    )
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    # Act
    response = client.get(url, HTTP_X_ORG_TOKEN=token.pk)

    # Assert
    assert response.status_code == 410
    assert "maximum number of uses" in response.json()["detail"].lower()


def test_expired_org_token_for_different_org_returns_404(
    client: Client, organization: Organization, organization_owner_user: RevelUser
) -> None:
    """GET /organizations/{slug} with an expired token for a *different* org returns 404.

    This is the info-leakage guard: the controller must not reveal the
    existence of org B just because the user holds a dead token for org A.
    """
    # Arrange -- token belongs to a *different* private organization
    other_org = Organization.objects.create(
        name="Other Private Org",
        slug="other-private-org",
        owner=organization_owner_user,
        visibility=Organization.Visibility.PRIVATE,
    )
    token = OrganizationToken.objects.create(
        organization=other_org,
        issuer=organization_owner_user,
        grants_membership=False,
        expires_at=timezone.now() - timedelta(hours=1),
    )
    organization.visibility = Organization.Visibility.PRIVATE
    organization.save()
    url = reverse("api:get_organization", kwargs={"slug": organization.slug})

    # Act
    response = client.get(url, HTTP_X_ORG_TOKEN=token.pk)

    # Assert -- must be 404, not 410
    assert response.status_code == 404
