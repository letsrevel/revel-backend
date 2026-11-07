"""MembershipTier.kind on the API and the TEMPORARY-tier model rules (#1320)."""

import typing as t

import orjson
import pytest
from django.core.exceptions import ValidationError
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    OrganizationToken,
)
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


def _json(client: Client, method: str, url: str, payload: dict[str, object]) -> t.Any:
    return getattr(client, method)(url, data=orjson.dumps(payload), content_type="application/json")


class TestKindOnTheApi:
    def test_create_defaults_to_standard(self, organization_owner_client: Client, organization: Organization) -> None:
        url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
        response = _json(organization_owner_client, "post", url, {"name": "Gold"})
        assert response.status_code == 201
        assert response.json()["kind"] == "standard"

    def test_create_temporary(self, organization_owner_client: Client, organization: Organization) -> None:
        url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
        response = _json(organization_owner_client, "post", url, {"name": "Day pass", "kind": "temporary"})
        assert response.status_code == 201
        assert response.json()["kind"] == "temporary"
        assert MembershipTier.objects.get(pk=response.json()["id"]).kind == MembershipTier.Kind.TEMPORARY

    def test_create_temporary_with_approval_requirement_is_rejected(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        url = reverse("api:create_membership_tier", kwargs={"slug": organization.slug})
        response = _json(
            organization_owner_client,
            "post",
            url,
            {"name": "Day pass", "kind": "temporary", "requires_membership_approval": True},
        )
        assert response.status_code == 400
        assert not MembershipTier.objects.filter(organization=organization, name="Day pass").exists()

    def test_update_cannot_change_kind(
        self, organization_owner_client: Client, organization: Organization, standard_tier: MembershipTier
    ) -> None:
        """The update schema has no ``kind``: it is ignored, the tier stays STANDARD."""
        url = reverse("api:update_membership_tier", kwargs={"slug": organization.slug, "tier_id": standard_tier.id})
        response = _json(organization_owner_client, "put", url, {"name": "Renamed", "kind": "temporary"})
        assert response.status_code == 200
        assert response.json()["kind"] == "standard"
        standard_tier.refresh_from_db()
        assert standard_tier.name == "Renamed"
        assert standard_tier.kind == MembershipTier.Kind.STANDARD

    def test_admin_listing_returns_all_tiers_with_kind(
        self,
        organization_owner_client: Client,
        organization: Organization,
        standard_tier: MembershipTier,
        temp_tier: MembershipTier,
    ) -> None:
        """The admin list is the data source of the ticket-tier grants picker: it keeps TEMPORARY tiers."""
        url = reverse("api:list_membership_tiers", kwargs={"slug": organization.slug})
        response = organization_owner_client.get(url)
        assert response.status_code == 200
        kinds = {row["id"]: row["kind"] for row in response.json()}
        assert kinds[str(standard_tier.id)] == "standard"
        assert kinds[str(temp_tier.id)] == "temporary"

    def test_reorder_works_over_all_tiers(
        self,
        organization_owner_client: Client,
        organization: Organization,
        standard_tier: MembershipTier,
        temp_tier: MembershipTier,
    ) -> None:
        url = reverse("api:reorder_membership_tiers", kwargs={"slug": organization.slug})
        others = [
            str(pk)
            for pk in organization.tiers.exclude(pk__in=[temp_tier.pk, standard_tier.pk]).values_list("pk", flat=True)
        ]
        ids = [str(temp_tier.id), str(standard_tier.id), *others]
        response = _json(organization_owner_client, "patch", url, {"tier_ids": ids})
        assert response.status_code == 204
        temp_tier.refresh_from_db()
        standard_tier.refresh_from_db()
        assert temp_tier.display_order < standard_tier.display_order


class TestTemporaryTierModelRules:
    def test_cannot_have_questionnaire(self, organization: Organization) -> None:
        questionnaire = Questionnaire.objects.create(name="Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED)
        oq = OrganizationQuestionnaire.objects.create(
            organization=organization,
            questionnaire=questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        )
        with pytest.raises(ValidationError):
            MembershipTier.objects.create(
                organization=organization,
                name="Day pass",
                kind=MembershipTier.Kind.TEMPORARY,
                membership_questionnaire=oq,
            )

    @pytest.mark.parametrize("requires_approval", [True, False])
    def test_cannot_set_approval_override(self, organization: Organization, requires_approval: bool) -> None:
        with pytest.raises(ValidationError):
            MembershipTier.objects.create(
                organization=organization,
                name="Day pass",
                kind=MembershipTier.Kind.TEMPORARY,
                requires_membership_approval=requires_approval,
            )

    def test_cannot_be_held_on_an_access_row(
        self, organization: Organization, member_user: RevelUser, temp_tier: MembershipTier
    ) -> None:
        """Day members get no OrganizationMember row, so wallet cards can never show a TEMPORARY tier."""
        member = OrganizationMember(organization=organization, user=member_user, tier=temp_tier)
        with pytest.raises(ValidationError) as exc:
            member.full_clean()
        assert "tier" in exc.value.message_dict

    def test_standard_tier_can_be_held(
        self, organization: Organization, member_user: RevelUser, standard_tier: MembershipTier
    ) -> None:
        OrganizationMember(organization=organization, user=member_user, tier=standard_tier).full_clean()

    def test_cannot_have_subscription_plans(self, temp_tier: MembershipTier) -> None:
        plan = MembershipSubscriptionPlan(
            tier=temp_tier, name="Monthly", price="5.00", currency="EUR", period_unit="month", period_count=1
        )
        with pytest.raises(ValidationError) as exc:
            plan.full_clean()
        assert "tier" in exc.value.message_dict

    def test_org_token_cannot_grant_it(
        self, organization: Organization, organization_owner_user: RevelUser, temp_tier: MembershipTier
    ) -> None:
        token = OrganizationToken(
            organization=organization,
            issuer=organization_owner_user,
            grants_membership=True,
            membership_tier=temp_tier,
        )
        with pytest.raises(ValidationError) as exc:
            token.clean()
        assert "membership_tier" in exc.value.message_dict
