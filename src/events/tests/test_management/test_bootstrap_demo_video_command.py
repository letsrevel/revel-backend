"""Tests for the ``bootstrap_demo_video`` management command."""

from io import StringIO

import pytest
from django.core.management import call_command

from accounts.models import RevelUser
from events.management.commands.demo_video_helpers import DEMO_EMAIL_DOMAIN
from events.models import (
    Event,
    EventInvitation,
    Organization,
    OrganizationMember,
    PotluckItem,
    TicketTier,
)
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission

pytestmark = pytest.mark.django_db

DEMO_ORG_SLUGS = {
    "shibari-circle-vienna",
    "the-velvet-cellar",
    "sunday-slow-picnic-club",
    "analog-photo-walks",
    "paper-hearts-book-club",
}

EvalStatus = QuestionnaireEvaluation.QuestionnaireEvaluationStatus


def _run() -> str:
    out = StringIO()
    call_command("bootstrap_demo_video", stdout=out)
    return out.getvalue()


def _demo_counts() -> dict[str, int]:
    """Row counts for everything the seed owns, keyed by model name."""
    demo_orgs = Organization.objects.filter(slug__in=DEMO_ORG_SLUGS)
    demo_events = Event.objects.filter(organization__in=demo_orgs)
    return {
        "users": RevelUser.objects.filter(email__endswith=f"@{DEMO_EMAIL_DOMAIN}").count(),
        "organizations": demo_orgs.count(),
        "events": demo_events.count(),
        "tiers": TicketTier.objects.filter(event__in=demo_events).count(),
        "potluck_items": PotluckItem.objects.filter(event__in=demo_events).count(),
        "invitations": EventInvitation.objects.filter(event__in=demo_events).count(),
        "memberships": OrganizationMember.objects.filter(organization__in=demo_orgs).count(),
        "submissions": QuestionnaireSubmission.objects.filter(user__email__endswith=f"@{DEMO_EMAIL_DOMAIN}").count(),
    }


class TestBootstrapDemoVideo:
    """Coverage for the demo-video scenario seed."""

    def test_seeds_the_five_scenarios(self) -> None:
        output = _run()

        assert set(Organization.objects.values_list("slug", flat=True)) == DEMO_ORG_SLUGS
        for slug in DEMO_ORG_SLUGS:
            assert f"/org/{slug}" in output

        # Scenario 1: a free, quantity-capped tier behind a manual questionnaire.
        workshop = Event.objects.get(slug="intro-to-shibari-rope-and-trust")
        assert list(workshop.ticket_tiers.values_list("name", flat=True)) == ["Workshop Spot"]
        assert workshop.ticket_tiers.get().total_quantity == 24
        assert (
            QuestionnaireSubmission.objects.filter(
                questionnaire__name="Workshop Application", evaluation__status=EvalStatus.PENDING_REVIEW
            ).count()
            == 3
        )
        # ...and one attendee deliberately left without a submission.
        newcomer = RevelUser.objects.get(email=f"noa.attendee@{DEMO_EMAIL_DOMAIN}")
        assert not newcomer.questionnaire_submissions.exists()

        # Scenario 2: a public door-price tier alongside a members-only free one.
        gig = Event.objects.get(slug="basement-sessions-live-and-loud")
        members_tier = gig.ticket_tiers.get(name="Members — Free Entry")
        assert members_tier.visibility == TicketTier.Visibility.MEMBERS_ONLY
        assert members_tier.purchasable_by == TicketTier.PurchasableBy.MEMBERS
        assert gig.ticket_tiers.get(name="General Admission").payment_method == (TicketTier.PaymentMethod.AT_THE_DOOR)

        # Scenario 3: an RSVP potluck with unclaimed suggestions and claimed items.
        picnic = Event.objects.get(slug="picnic-in-the-park")
        assert picnic.requires_ticket is False
        assert picnic.potluck_open is True
        assert picnic.potluck_items.filter(is_suggested=True, assignee__isnull=True).count() == 6
        assert picnic.potluck_items.filter(assignee__isnull=False).count() == 4

        # Scenario 4: enough evaluated submissions for the insights views.
        walk_evaluations = QuestionnaireEvaluation.objects.filter(submission__questionnaire__name="Walk Application")
        assert walk_evaluations.filter(status=EvalStatus.APPROVED).count() == 5
        assert walk_evaluations.filter(status=EvalStatus.REJECTED).count() == 2
        assert walk_evaluations.filter(status=EvalStatus.PENDING_REVIEW).count() == 3

        # Scenario 5: an invitation that waives both gates.
        invitation = EventInvitation.objects.get(event__slug="monthly-reading-circle")
        assert invitation.waives_questionnaire is True
        assert invitation.waives_membership_required is True
        assert Event.objects.get(slug="monthly-reading-circle").event_type == Event.EventType.MEMBERS_ONLY

    def test_is_idempotent(self) -> None:
        """A second run must refresh the same rows, never duplicate them."""
        _run()
        first = _demo_counts()

        _run()

        assert _demo_counts() == first

    def test_every_demo_account_signs_in_with_the_shared_password(self) -> None:
        _run()

        users = RevelUser.objects.filter(email__endswith=f"@{DEMO_EMAIL_DOMAIN}")
        assert users.count() == 29
        assert all(user.check_password("password123") and user.email_verified for user in users)
