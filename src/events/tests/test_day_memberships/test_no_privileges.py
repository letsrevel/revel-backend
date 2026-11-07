"""A day member holds a register record only: no platform privileges (#1320).

Every membership-gated feature reads ``OrganizationMember``; a person with just a TEMPORARY
``MembershipRecord`` must be treated as a non-member by all of them.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    Announcement,
    Blacklist,
    Event,
    MembershipRecord,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationQuestionnaire,
    TicketTier,
)
from events.service import announcement_service, tier_eligibility
from events.service.event_manager import EligibilityService
from events.service.event_manager.enums import Reasons
from notifications.service.eligibility import BatchParticipationChecker, is_org_member
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


@pytest.fixture
def day_member(holder: RevelUser, organization: Organization, day_member_tier: MembershipTier) -> RevelUser:
    """A person who is a day member right now, with no access row."""
    MembershipRecord.objects.create(
        organization=organization,
        user=holder,
        full_name="Dana Day",
        kind=MembershipRecord.Kind.TEMPORARY,
        tier=day_member_tier,
        tier_name=day_member_tier.name,
        valid_from=timezone.now() - timedelta(hours=1),
        valid_until=timezone.now() + timedelta(hours=5),
        source=MembershipRecord.Source.MANUAL,
    )
    assert not OrganizationMember.objects.filter(user=holder).exists()
    return holder


def test_membership_gate_blocks_day_member(day_member: RevelUser, members_only_event: Event) -> None:
    result = EligibilityService(day_member, members_only_event).check_eligibility()
    assert result.allowed is False


def test_blacklist_gate_gives_day_member_no_member_bypass(
    day_member: RevelUser, organization: Organization, organization_owner_user: RevelUser, public_event: Event
) -> None:
    """Fuzzy-matched members skip verification; a day member must still be asked to verify."""
    Blacklist.objects.create(
        organization=organization, first_name="Dana", last_name="Day", created_by=organization_owner_user
    )
    result = EligibilityService(day_member, public_event).check_eligibility()
    assert result.allowed is False
    assert result.reason_code == Reasons.VERIFICATION_REQUIRED.code


def test_members_exempt_questionnaire_still_required_of_day_member(
    day_member: RevelUser, organization: Organization, public_event: Event
) -> None:
    questionnaire = Questionnaire.objects.create(
        name="Exempt for members", status=Questionnaire.QuestionnaireStatus.PUBLISHED
    )
    org_q = OrganizationQuestionnaire.objects.create(
        organization=organization,
        questionnaire=questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        members_exempt=True,
        requires_evaluation=False,
    )
    org_q.events.set([public_event])
    result = EligibilityService(day_member, public_event).check_eligibility()
    assert result.allowed is False
    assert result.reason_code == Reasons.QUESTIONNAIRE_MISSING.code


def test_members_tier_not_purchasable_or_visible_to_day_member(
    day_member: RevelUser, public_event: Event, members_only_event: Event
) -> None:
    purchasable = TicketTier.objects.create(
        event=public_event,
        name="Members buy",
        price=Decimal("5.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        purchasable_by=TicketTier.PurchasableBy.MEMBERS,
    )
    visible = TicketTier.objects.create(
        event=public_event,
        name="Members see",
        price=Decimal("5.00"),
        payment_method=TicketTier.PaymentMethod.FREE,
        visibility=TicketTier.Visibility.MEMBERS_ONLY,
    )
    eligible = {tier.pk for tier in tier_eligibility.get_eligible_tiers(public_event, day_member)}
    assert purchasable.pk not in eligible
    assert visible.pk not in eligible
    assert visible.pk not in set(TicketTier.objects.for_user(day_member).values_list("pk", flat=True))


def test_members_only_events_not_listed_for_day_member(day_member: RevelUser, members_only_event: Event) -> None:
    assert members_only_event.pk not in set(Event.objects.for_user(day_member).values_list("pk", flat=True))


def test_member_announcements_skip_day_member(
    day_member: RevelUser, organization: Organization, organization_owner_user: RevelUser
) -> None:
    announcement = Announcement.objects.create(
        organization=organization,
        title="Members only",
        body="Hello",
        target_all_members=True,
        created_by=organization_owner_user,
    )
    assert day_member.pk not in set(announcement_service.get_recipients(announcement).values_list("pk", flat=True))


def test_member_notification_eligibility_skips_day_member(
    day_member: RevelUser, organization: Organization, public_event: Event
) -> None:
    assert is_org_member(day_member, organization) is False
    assert BatchParticipationChecker(public_event).is_org_member(day_member.pk) is False
