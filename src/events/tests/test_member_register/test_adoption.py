"""Prevention at the source: opening a STANDARD record first adopts a verifiably matching unlinked one (#1319).

Covers the approval, ``add_member`` and token-claim paths (all reach ``open_standard_record`` through the
mirror), the staff alert, tier and evidence handling, flag-only matches, and approve-and-link.
"""

import typing as t
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationToken,
)
from events.service import member_register_service
from events.service.organization_service import membership as membership_service
from events.service.organization_service.tokens import claim_invitation
from notifications.enums import NotificationType
from questionnaires.models import Questionnaire, QuestionnaireSubmission

from .conftest import RecordFactory

pytestmark = pytest.mark.django_db

ALERT = NotificationType.STAFF_MEMBER_RECORD_AUTO_LINKED


@pytest.fixture
def other_tier(organization: Organization) -> MembershipTier:
    """A second tier in the organization."""
    return MembershipTier.objects.create(organization=organization, name="Silver")


def _user(django_user_model: type[RevelUser], email: str = "pat@example.com", **extra: t.Any) -> RevelUser:
    extra.setdefault("email_verified", True)
    return django_user_model.objects.create_user(username=email, email=email, password="pw", **extra)


def _open_records(organization: Organization, user: RevelUser) -> list[MembershipRecord]:
    return list(
        MembershipRecord.objects.filter(organization=organization, user=user, valid_until__isnull=True).order_by("pk")
    )


def _alerts(send: t.Any) -> list[t.Any]:
    return [c for c in send.call_args_list if c.kwargs.get("notification_type") == ALERT]


def _application(organization: Organization, user: RevelUser, tier: MembershipTier) -> OrganizationMembershipRequest:
    return OrganizationMembershipRequest.objects.create(organization=organization, user=user, tier=tier)


class TestAdoptionAtSource:
    def test_future_dated_record_is_not_adopted(
        self,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        """An entry that starts in the future can't back access starting now: a new record opens instead."""
        future = make_record(valid_from=timezone.now() + timedelta(days=10))
        user = _user(django_user_model)
        application = _application(organization, user, link_tier)

        membership_service.approve_membership_request(application, decided_by=organization_owner_user)

        future.refresh_from_db()
        assert future.user_id is None
        opened = _open_records(organization, user)
        assert len(opened) == 1 and opened[0].pk != future.pk

    def test_approval_adopts_verified_email_match(
        self,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        record = make_record(email="PAT@example.com")
        user = _user(django_user_model)
        application = _application(organization, user, link_tier)

        with patch("notifications.signals.notification_requested.send") as send:
            with django_capture_on_commit_callbacks(execute=True):
                membership_service.approve_membership_request(application, decided_by=organization_owner_user)

        record.refresh_from_db()
        assert record.user == user
        assert _open_records(organization, user) == [record]
        assert MembershipRecord.objects.filter(organization=organization).count() == 1
        alerts = _alerts(send)
        assert len(alerts) == 1 and alerts[0].kwargs["context"]["record_id"] == str(record.pk)

    def test_adopts_the_earliest_record(
        self,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        now = timezone.now()
        later = make_record(valid_from=now - timedelta(days=1))
        earlier = make_record(valid_from=now - timedelta(days=10))
        user = _user(django_user_model)

        membership_service.approve_membership_request(
            _application(organization, user, link_tier), decided_by=organization_owner_user
        )

        earlier.refresh_from_db()
        later.refresh_from_db()
        assert earlier.user == user and later.user is None

    def test_add_member_adopts(
        self,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        record = make_record()
        user = _user(django_user_model)

        membership_service.add_member(organization, user, link_tier)

        record.refresh_from_db()
        assert record.user == user
        assert _open_records(organization, user) == [record]

    def test_token_claim_adopts(
        self,
        organization: Organization,
        make_record: RecordFactory,
        django_user_model: type[RevelUser],
        organization_token: OrganizationToken,
    ) -> None:
        record = make_record(tier=organization_token.membership_tier)
        user = _user(django_user_model)

        assert claim_invitation(user, organization_token.id) is not None

        record.refresh_from_db()
        assert record.user == user
        assert _open_records(organization, user) == [record]

    def test_tier_difference_chains_tier_changed(
        self,
        organization: Organization,
        make_record: RecordFactory,
        other_tier: MembershipTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        original_from = timezone.now() - timedelta(days=30)
        record = make_record(valid_from=original_from)
        user = _user(django_user_model)

        membership_service.approve_membership_request(
            _application(organization, user, other_tier), decided_by=organization_owner_user
        )

        record.refresh_from_db()
        assert record.user == user
        assert record.valid_from == original_from
        assert record.valid_until is not None
        assert record.ended_reason == MembershipRecord.EndedReason.TIER_CHANGED
        (open_record,) = _open_records(organization, user)
        assert open_record.pk != record.pk
        assert open_record.tier == other_tier
        assert open_record.source == MembershipRecord.Source.APPLICATION

    def test_equal_tier_copies_missing_evidence(
        self,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        record = make_record()
        user = _user(django_user_model)
        questionnaire = Questionnaire.objects.create(
            name="Vetting Q", status=Questionnaire.QuestionnaireStatus.PUBLISHED
        )
        submission = QuestionnaireSubmission.objects.create(
            user=user,
            questionnaire=questionnaire,
            status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            submitted_at=timezone.now(),
        )
        application = OrganizationMembershipRequest.objects.create(
            organization=organization, user=user, tier=link_tier, questionnaire_submission=submission
        )

        membership_service.approve_membership_request(application, decided_by=organization_owner_user)

        record.refresh_from_db()
        assert record.user == user
        assert record.questionnaire_submission_id == submission.pk
        assert record.questionnaire_name == "Vetting Q"
        assert record.accepted_at == submission.submitted_at
        assert _open_records(organization, user) == [record]

    def test_unverified_email_does_not_adopt(
        self,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        record = make_record()
        user = _user(django_user_model, email_verified=False)

        membership_service.approve_membership_request(
            _application(organization, user, link_tier), decided_by=organization_owner_user
        )

        record.refresh_from_db()
        assert record.user is None
        (open_record,) = _open_records(organization, user)
        assert open_record.pk != record.pk

    def test_guest_never_adopts(
        self, organization: Organization, make_record: RecordFactory, link_tier: MembershipTier
    ) -> None:
        record = make_record()
        guest = RevelUser.objects.create_user(
            username="guest-pat", email="pat@example.com", password="pw", email_verified=True, guest=True
        )

        member_register_service.open_standard_record(
            OrganizationMember.objects.create(organization=organization, user=guest, tier=link_tier)
        )

        record.refresh_from_db()
        assert record.user is None

    def test_closed_or_linked_records_are_not_adopted(
        self,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
        organization_owner_user: RevelUser,
    ) -> None:
        closed = make_record(
            valid_from=timezone.now() - timedelta(days=9),
            valid_until=timezone.now() - timedelta(days=1),
        )
        user = _user(django_user_model)

        membership_service.approve_membership_request(
            _application(organization, user, link_tier), decided_by=organization_owner_user
        )

        closed.refresh_from_db()
        assert closed.user is None


class TestRegisterMatches:
    def _url(self, organization: Organization, application: OrganizationMembershipRequest) -> str:
        return reverse(
            "api:list_membership_request_register_matches",
            kwargs={"slug": organization.slug, "request_id": application.pk},
        )

    def test_unverified_email_is_listed_with_reason(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        record = make_record(full_name="Someone Else")
        user = _user(django_user_model, email_verified=False)

        response = owner_client.get(self._url(organization, _application(organization, user, link_tier)))

        assert response.status_code == 200, response.content
        (match,) = response.json()
        assert match["record"]["id"] == str(record.pk)
        assert match["reasons"] == ["email"]

    def test_name_match_is_listed_accent_insensitively(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        record = make_record(full_name="  josé   GARCÍA ", email="")
        user = _user(django_user_model, email="jose@example.com", first_name="Jose", last_name="Garcia")

        response = owner_client.get(self._url(organization, _application(organization, user, link_tier)))

        (match,) = response.json()
        assert match["record"]["id"] == str(record.pk)
        assert match["reasons"] == ["name"]

    def test_claim_match_and_exclusions(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        user = _user(django_user_model, email="applicant@example.com")
        claimed = make_record(full_name="X", email="", duplicate_claimant=user)
        make_record(full_name="Unrelated", email="nobody@example.com")
        make_record(full_name="Closed", email="applicant@example.com", valid_until=timezone.now() + timedelta(hours=1))
        make_record(full_name="Linked", email="applicant@example.com", user=_user(django_user_model, email="l@x.io"))

        response = owner_client.get(self._url(organization, _application(organization, user, link_tier)))

        (match,) = response.json()
        assert match["record"]["id"] == str(claimed.pk) and match["reasons"] == ["claim"]

    def test_request_of_another_org_is_404(
        self,
        owner_client: Client,
        organization: Organization,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        user = _user(django_user_model)
        other = Organization.objects.create(name="Other", slug="other", owner=_user(django_user_model, email="o@x.io"))
        foreign = OrganizationMembershipRequest.objects.create(
            organization=other, user=user, tier=MembershipTier.objects.create(organization=other, name="T")
        )

        assert owner_client.get(self._url(organization, foreign)).status_code == 404

    @pytest.mark.parametrize(
        ("client_fixture", "expected"),
        [
            ("owner_client", 200),
            ("staff_client", 200),
            ("plain_staff_client", 403),
            ("outsider_client", 404),
            ("anonymous_client", 401),
        ],
    )
    def test_permission_matrix(
        self,
        client_fixture: str,
        expected: int,
        request: pytest.FixtureRequest,
        organization: Organization,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        client: Client = Client() if client_fixture == "anonymous_client" else request.getfixturevalue(client_fixture)
        application = _application(organization, _user(django_user_model), link_tier)

        assert client.get(self._url(organization, application)).status_code == expected


class TestApproveAndLink:
    def _approve(self, client: Client, organization: Organization, application: t.Any, **body: t.Any) -> t.Any:
        url = reverse(
            "api:approve_membership_request", kwargs={"slug": organization.slug, "request_id": application.pk}
        )
        return client.post(url, data=body, content_type="application/json")

    def test_links_chosen_record(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        record = make_record(email="different@example.com", full_name="Totally Different")
        user = _user(django_user_model, email_verified=False)
        application = _application(organization, user, link_tier)

        response = self._approve(owner_client, organization, application, link_record_id=str(record.pk))

        assert response.status_code == 204, response.content
        record.refresh_from_db()
        assert record.user == user
        assert _open_records(organization, user) == [record]
        assert OrganizationMember.objects.get(organization=organization, user=user).status == "active"

    def test_tier_difference_chains_from_linked_record(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        other_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        record = make_record(email="different@example.com")
        user = _user(django_user_model, email_verified=False)
        application = _application(organization, user, other_tier)

        assert self._approve(owner_client, organization, application, link_record_id=str(record.pk)).status_code == 204

        record.refresh_from_db()
        assert record.user == user and record.ended_reason == MembershipRecord.EndedReason.TIER_CHANGED
        (open_record,) = _open_records(organization, user)
        assert open_record.tier == other_tier

    def test_paid_application_is_refused(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        from decimal import Decimal

        from events.models import MembershipSubscriptionPlan, SubscriptionPaymentMethod

        plan = MembershipSubscriptionPlan.objects.create(
            tier=link_tier,
            name="Plan",
            price=Decimal("10.00"),
            currency="EUR",
            period_unit="month",
            payment_method=SubscriptionPaymentMethod.OFFLINE,
        )
        record = make_record(email="different@example.com")
        user = _user(django_user_model)
        application = OrganizationMembershipRequest.objects.create(
            organization=organization, user=user, tier=link_tier, plan=plan
        )

        response = self._approve(owner_client, organization, application, link_record_id=str(record.pk))

        assert response.status_code == 400
        record.refresh_from_db()
        application.refresh_from_db()
        assert record.user is None and application.status == OrganizationMembershipRequest.Status.PENDING

    def test_record_of_another_org_is_404(
        self,
        owner_client: Client,
        organization: Organization,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        other_owner = _user(django_user_model, email="o2@example.com")
        other_org = Organization.objects.create(name="Other", slug="other", owner=other_owner)
        foreign = MembershipRecord.objects.create(
            organization=other_org,
            full_name="Foreign",
            valid_from=timezone.now(),
            source=MembershipRecord.Source.MANUAL,
        )
        user = _user(django_user_model)
        application = _application(organization, user, link_tier)

        response = self._approve(owner_client, organization, application, link_record_id=str(foreign.pk))

        assert response.status_code == 404

    @pytest.mark.parametrize("kind", ["linked", "merged", "closed"])
    def test_unlinkable_records_are_400(
        self,
        kind: str,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        survivor = make_record(email="s@example.com")
        extras: dict[str, dict[str, t.Any]] = {
            "linked": {"user": _user(django_user_model, email="l@example.com")},
            "merged": {"merged_into": survivor},
            "closed": {"valid_until": timezone.now() + timedelta(hours=1)},
        }
        extra = extras[kind]
        record = make_record(email="different@example.com", **extra)
        user = _user(django_user_model)
        application = _application(organization, user, link_tier)

        response = self._approve(owner_client, organization, application, link_record_id=str(record.pk))

        assert response.status_code == 400
        application.refresh_from_db()
        assert application.status == OrganizationMembershipRequest.Status.PENDING

    def test_applicant_with_open_record_is_400(
        self,
        owner_client: Client,
        organization: Organization,
        make_record: RecordFactory,
        link_tier: MembershipTier,
        django_user_model: type[RevelUser],
    ) -> None:
        user = _user(django_user_model, email_verified=False)
        MembershipRecord.objects.create(
            organization=organization,
            user=user,
            full_name="Own",
            valid_from=timezone.now(),
            source=MembershipRecord.Source.MANUAL,
        )
        record = make_record(email="different@example.com")
        application = _application(organization, user, link_tier)

        response = self._approve(owner_client, organization, application, link_record_id=str(record.pk))

        assert response.status_code == 400
