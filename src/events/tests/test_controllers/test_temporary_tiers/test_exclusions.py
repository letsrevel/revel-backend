"""TEMPORARY tiers are excluded from every place a tier is held or targeted (#1320)."""

import typing as t
import uuid
from datetime import timedelta

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Announcement,
    Event,
    MembershipSubscriptionPlan,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationQuestionnaire,
    TicketTier,
)
from events.models.mixins import ResourceVisibility
from events.schema import AnnouncementCreateSchema
from events.service import announcement_service
from events.service.event_manager import EligibilityService
from polls.exceptions import PollValidationError
from polls.models import Poll
from polls.schema import PollCreateSchema
from polls.service import poll_service
from questionnaires.models import Questionnaire, QuestionnaireSubmission

pytestmark = pytest.mark.django_db


def _json(client: Client, method: str, url: str, payload: dict[str, t.Any]) -> t.Any:
    return getattr(client, method)(url, data=orjson.dumps(payload), content_type="application/json")


class TestPublicListings:
    def test_membership_tiers_listing_hides_temporary(
        self, client: Client, public_org: Organization, standard_tier: MembershipTier, temp_tier: MembershipTier
    ) -> None:
        url = reverse("api:list_organization_membership_tiers", kwargs={"slug": public_org.slug})
        response = client.get(url)
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()}
        assert str(standard_tier.id) in ids
        assert str(temp_tier.id) not in ids

    def test_membership_plans_listing_hides_temporary(
        self, client: Client, public_org: Organization, standard_tier: MembershipTier, temp_tier: MembershipTier
    ) -> None:
        # bulk_create skips full_clean, i.e. the model rule that forbids plans on a TEMPORARY tier.
        plans = MembershipSubscriptionPlan.objects.bulk_create(
            [
                MembershipSubscriptionPlan(
                    tier=tier, name="Monthly", price="5.00", currency="EUR", period_unit="month", period_count=1
                )
                for tier in (standard_tier, temp_tier)
            ]
        )
        url = reverse("api:list_organization_membership_plans", kwargs={"slug": public_org.slug})
        response = client.get(url)
        assert response.status_code == 200
        ids = {row["id"] for row in response.json()}
        assert str(plans[0].id) in ids
        assert str(plans[1].id) not in ids


class TestApplicationFlow:
    def test_apply_for_temporary_tier_is_404(
        self, nonmember_api_client: Client, organization: Organization, temp_tier: MembershipTier
    ) -> None:
        url = reverse("api:apply_for_membership", kwargs={"slug": organization.slug})
        response = _json(nonmember_api_client, "post", url, {"tier_id": str(temp_tier.id)})
        assert response.status_code == 404
        assert not OrganizationMembershipRequest.objects.filter(organization=organization).exists()

    def test_apply_for_standard_tier_still_works(
        self, nonmember_api_client: Client, public_org: Organization, standard_tier: MembershipTier
    ) -> None:
        url = reverse("api:apply_for_membership", kwargs={"slug": public_org.slug})
        response = _json(nonmember_api_client, "post", url, {"tier_id": str(standard_tier.id)})
        assert response.status_code == 201

    def test_join_eligibility_for_temporary_tier_is_404(
        self, nonmember_api_client: Client, organization: Organization, temp_tier: MembershipTier
    ) -> None:
        url = reverse("api:get_join_eligibility", kwargs={"slug": organization.slug})
        response = nonmember_api_client.get(url, {"tier_id": str(temp_tier.id)})
        assert response.status_code == 404

    def test_questionnaire_review_candidates_skip_temporary_tiers(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        temp_tier: MembershipTier,
    ) -> None:
        """A TEMPORARY tier inherits the org-default questionnaire; it must not win the candidate list."""
        questionnaire = Questionnaire.objects.create(
            name="Membership Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED
        )
        oq = OrganizationQuestionnaire.objects.create(
            organization=organization,
            questionnaire=questionnaire,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
            requires_evaluation=True,
        )
        organization.default_membership_questionnaire = oq
        organization.save(update_fields=["default_membership_questionnaire"])
        QuestionnaireSubmission.objects.create(
            user=member_user,
            questionnaire=questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
        )
        # Sort the temporary tier first so, unfiltered, it would be picked for the questionnaire.
        MembershipTier.objects.filter(organization=organization, kind=MembershipTier.Kind.STANDARD).update(
            display_order=5
        )
        MembershipTier.objects.filter(pk=temp_tier.pk).update(display_order=0)

        result = EligibilityService(member_user, event)._tiers_awaiting_questionnaire_review([])

        assert result
        assert all(tier is None or tier.kind == MembershipTier.Kind.STANDARD for tier in result)


class TestAdminMemberFlows:
    def test_add_member_with_temporary_tier_is_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        temp_tier: MembershipTier,
    ) -> None:
        url = reverse(
            "api:create_organization_member", kwargs={"slug": organization.slug, "user_id": nonmember_user.id}
        )
        response = _json(organization_owner_client, "post", url, {"tier_id": str(temp_tier.id)})
        assert response.status_code == 404
        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_update_member_to_temporary_tier_is_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        member_user: RevelUser,
        standard_tier: MembershipTier,
        temp_tier: MembershipTier,
    ) -> None:
        member = OrganizationMember.objects.create(organization=organization, user=member_user, tier=standard_tier)
        url = reverse("api:update_organization_member", kwargs={"slug": organization.slug, "user_id": member_user.id})
        response = _json(organization_owner_client, "put", url, {"tier_id": str(temp_tier.id)})
        assert response.status_code == 404
        member.refresh_from_db()
        assert member.tier == standard_tier

    def test_approve_request_with_temporary_tier_is_404(
        self,
        organization_owner_client: Client,
        organization: Organization,
        nonmember_user: RevelUser,
        standard_tier: MembershipTier,
        temp_tier: MembershipTier,
    ) -> None:
        request = OrganizationMembershipRequest.objects.create(
            organization=organization, user=nonmember_user, tier=standard_tier
        )
        url = reverse("api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": request.id})
        response = _json(organization_owner_client, "post", url, {"tier_id": str(temp_tier.id)})
        assert response.status_code == 404
        assert not OrganizationMember.objects.filter(organization=organization, user=nonmember_user).exists()

    def test_register_record_with_temporary_tier_is_404(
        self, organization_owner_client: Client, organization: Organization, temp_tier: MembershipTier
    ) -> None:
        from django.http import Http404

        from events.service import member_register_admin_service

        with pytest.raises(Http404):
            member_register_admin_service._resolve_tier(organization, temp_tier.id)


class TestSubscriptionPlans:
    def test_create_plan_on_temporary_tier_is_404(
        self, organization_owner_client: Client, organization: Organization, temp_tier: MembershipTier
    ) -> None:
        url = reverse("api:create_subscription_plan", kwargs={"slug": organization.slug, "tier_id": temp_tier.id})
        payload = {"name": "Monthly", "price": "5.00", "currency": "EUR", "period_unit": "month"}
        response = _json(organization_owner_client, "post", url, payload)
        assert response.status_code == 404
        assert not MembershipSubscriptionPlan.objects.filter(tier=temp_tier).exists()


class TestOrganizationTokens:
    def test_token_with_temporary_tier_is_404(
        self, organization_owner_client: Client, organization: Organization, temp_tier: MembershipTier
    ) -> None:
        url = reverse("api:create_organization_token", kwargs={"slug": organization.slug})
        payload = {
            "name": "Tok",
            "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            "grants_membership": True,
            "membership_tier_id": str(temp_tier.id),
        }
        response = _json(organization_owner_client, "post", url, payload)
        assert response.status_code == 404


class TestTicketTierMembershipLinks:
    def _create(self, client: Client, event: Event, **extra: t.Any) -> t.Any:
        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Tier", "price": "0", "currency": "EUR", **extra}
        return _json(client, "post", url, payload)

    def test_restricted_to_temporary_tier_is_404(
        self, organization_owner_client: Client, event: Event, temp_tier: MembershipTier
    ) -> None:
        response = self._create(
            organization_owner_client,
            event,
            purchasable_by="members",
            restricted_to_membership_tiers_ids=[str(temp_tier.id)],
        )
        assert response.status_code == 404

    def test_excluded_temporary_tier_is_404(
        self, organization_owner_client: Client, event: Event, temp_tier: MembershipTier
    ) -> None:
        response = self._create(
            organization_owner_client,
            event,
            purchasable_by="non_members",
            excluded_membership_tiers_ids=[str(temp_tier.id)],
        )
        assert response.status_code == 404

    def test_excluded_standard_tier_is_accepted(
        self, organization_owner_client: Client, event: Event, standard_tier: MembershipTier
    ) -> None:
        response = self._create(
            organization_owner_client,
            event,
            purchasable_by="non_members",
            excluded_membership_tiers_ids=[str(standard_tier.id)],
        )
        assert response.status_code == 200, response.content

    @pytest.mark.parametrize("field", ["restricted_to_membership_tiers", "excluded_membership_tiers"])
    def test_model_rejects_temporary_tier_in_either_m2m(
        self, event: Event, temp_tier: MembershipTier, field: str
    ) -> None:
        """Model-level guard for writes that bypass the service filter (admin's picker is limited separately)."""
        from django.core.exceptions import ValidationError

        tier = TicketTier.objects.create(
            event=event,
            name="T",
            purchasable_by=(
                TicketTier.PurchasableBy.MEMBERS
                if field == "restricted_to_membership_tiers"
                else TicketTier.PurchasableBy.NON_MEMBERS
            ),
        )
        getattr(tier, field).add(temp_tier)
        with pytest.raises(ValidationError) as exc:
            tier.full_clean()
        assert field.replace("restricted_to_membership_tiers", "restricted_to_tiers") in exc.value.message_dict


def _poll_payload(tier_ids: list[uuid.UUID]) -> PollCreateSchema:
    return PollCreateSchema(
        name="P",
        vote_visibility=ResourceVisibility.PUBLIC,
        result_visibility=ResourceVisibility.PUBLIC,
        result_timing=Poll.PollResultTiming.AFTER_VOTE,
        vote_membership_tier_ids=tier_ids,
    )


class TestPolls:
    def test_poll_tier_gate_rejects_temporary(self, organization: Organization, temp_tier: MembershipTier) -> None:
        payload = _poll_payload([temp_tier.id])
        with pytest.raises(PollValidationError):
            poll_service.create_poll(organization, payload)

    def test_poll_tier_gate_accepts_standard(self, organization: Organization, standard_tier: MembershipTier) -> None:
        payload = _poll_payload([standard_tier.id])
        poll = poll_service.create_poll(organization, payload)
        assert list(poll.vote_membership_tiers.all()) == [standard_tier]


class TestAnnouncements:
    def test_temporary_tier_id_is_rejected_on_create_without_writing(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        standard_tier: MembershipTier,
        temp_tier: MembershipTier,
    ) -> None:
        """A TEMPORARY tier id is rejected up front; no announcement is created."""
        bad_id = temp_tier.id
        with pytest.raises(ValueError, match="cannot be targeted by announcements"):
            announcement_service.create_announcement(
                organization,
                organization_owner_user,
                AnnouncementCreateSchema(title="Hi", body="Body", target_tier_ids=[standard_tier.id, bad_id]),
            )
        assert not Announcement.objects.filter(organization=organization).exists()

    def test_update_rejects_temporary_tier_and_keeps_existing_targets(
        self,
        organization: Organization,
        organization_owner_user: RevelUser,
        standard_tier: MembershipTier,
        temp_tier: MembershipTier,
    ) -> None:
        from events.schema import AnnouncementUpdateSchema

        announcement = announcement_service.create_announcement(
            organization,
            organization_owner_user,
            AnnouncementCreateSchema(title="Hi", body="Body", target_tier_ids=[standard_tier.id]),
        )
        with pytest.raises(ValueError, match="cannot be targeted by announcements"):
            announcement_service.update_announcement(
                announcement, AnnouncementUpdateSchema(target_tier_ids=[temp_tier.id])
            )
        assert list(announcement.target_tiers.all()) == [standard_tier]

    def test_create_api_returns_422_for_temporary_tier(
        self, organization: Organization, organization_owner_client: Client, temp_tier: MembershipTier
    ) -> None:
        url = reverse("api:create_announcement", kwargs={"slug": organization.slug})
        response = organization_owner_client.post(
            url,
            data={"title": "Hi", "body": "Body", "target_tier_ids": [str(temp_tier.id)]},
            content_type="application/json",
        )
        assert response.status_code == 422


class TestAdminPickers:
    @pytest.mark.parametrize("field", ["restricted_to_membership_tiers", "excluded_membership_tiers"])
    def test_ticket_tier_admin_form_excludes_temporary_tiers(
        self, field: str, standard_tier: MembershipTier, temp_tier: MembershipTier, superuser: RevelUser
    ) -> None:
        """Admin saves M2Ms after full_clean, so the picker itself must not offer TEMPORARY tiers."""
        from django.contrib import admin
        from django.test import RequestFactory

        def _admin_request(user: RevelUser) -> t.Any:
            request = RequestFactory().get("/")
            request.user = user
            return request

        model_admin = admin.site._registry[TicketTier]
        form = model_admin.get_form(_admin_request(superuser))()
        offered = set(t.cast(t.Any, form.fields[field]).queryset)
        assert standard_tier in offered
        assert temp_tier not in offered

    def test_application_model_rejects_temporary_tier(
        self, organization: Organization, nonmember_user: RevelUser, temp_tier: MembershipTier
    ) -> None:
        from django.core.exceptions import ValidationError

        request = OrganizationMembershipRequest(organization=organization, user=nonmember_user, tier=temp_tier)
        with pytest.raises(ValidationError) as exc:
            request.full_clean()
        assert "tier" in exc.value.message_dict
