"""Tests for the ``bootstrap_demo_video`` management command."""

import typing as t
from io import StringIO

import pytest
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from accounts.models import RevelUser
from common.thumbnails.service import ThumbnailResult
from events.management.commands import bootstrap_demo_video
from events.management.commands.demo_video_helpers import DEMO_EMAIL_DOMAIN, SCENARIOS, ScenarioSummary
from events.management.commands.demo_video_helpers.artwork import COVER_STORAGE_PREFIX, LOGO_STORAGE_PREFIX
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

DEMO_EVENT_SLUGS = {
    "intro-to-shibari-rope-and-trust",
    "basement-sessions-live-and-loud",
    "picnic-in-the-park",
    "golden-hour-photo-walk",
    "monthly-reading-circle",
}

EvalStatus = QuestionnaireEvaluation.QuestionnaireEvaluationStatus


@pytest.fixture(autouse=True)
def isolated_media(settings: t.Any, tmp_path: t.Any) -> None:
    """Keep the seeded artwork out of the repo's media directory."""
    settings.MEDIA_ROOT = tmp_path


def _run() -> str:
    out = StringIO()
    call_command("bootstrap_demo_video", stdout=out)
    return out.getvalue()


def _stored(prefix: str) -> list[str]:
    """Every file the seed wrote under ``prefix`` (thumbnails share the directory)."""
    _, files = default_storage.listdir(prefix)
    return sorted(files)


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

    @override_settings(DEMO_MODE=True)
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

    @override_settings(DEMO_MODE=True)
    def test_every_org_gets_a_logo_and_every_event_cover_art(self) -> None:
        """The scenarios are recorded on camera, so nothing may render bare."""
        output = _run()

        for org in Organization.objects.filter(slug__in=DEMO_ORG_SLUGS):
            assert org.logo.name == f"{LOGO_STORAGE_PREFIX}/{org.slug}.jpg"
            assert org.logo_thumbnail.name, f"{org.slug} has no logo thumbnail"
            assert default_storage.exists(org.logo.name)
            assert default_storage.exists(org.logo_thumbnail.name)

        for event in Event.objects.filter(organization__slug__in=DEMO_ORG_SLUGS):
            assert event.cover_art.name == f"{COVER_STORAGE_PREFIX}/{event.slug}.jpg"
            # cover_art_social is the rendition the frontend's hero actually reads.
            assert event.cover_art_thumbnail.name, f"{event.slug} has no cover thumbnail"
            assert event.cover_art_social.name, f"{event.slug} has no social cover"
            assert default_storage.exists(event.cover_art.name)
            assert default_storage.exists(event.cover_art_thumbnail.name)
            assert default_storage.exists(event.cover_art_social.name)

        assert "cover art" in output

    @override_settings(DEMO_MODE=True)
    def test_is_idempotent(self) -> None:
        """A second run must refresh the same rows, never duplicate them."""
        _run()
        first = _demo_counts()

        _run()

        assert _demo_counts() == first

    @override_settings(DEMO_MODE=True)
    def test_a_second_run_relinks_the_artwork_instead_of_re_uploading_it(self) -> None:
        """Storage must not accumulate ``<slug>_A1b2C3.jpg`` copies on every reseed."""
        _run()
        logos, covers = _stored(LOGO_STORAGE_PREFIX), _stored(COVER_STORAGE_PREFIX)
        assert logos == sorted(f"{slug}{suffix}.jpg" for slug in DEMO_ORG_SLUGS for suffix in ("", "_thumbnail"))
        assert covers == sorted(
            f"{slug}{suffix}.jpg" for slug in DEMO_EVENT_SLUGS for suffix in ("", "_thumbnail", "_social")
        )

        _run()

        assert (_stored(LOGO_STORAGE_PREFIX), _stored(COVER_STORAGE_PREFIX)) == (logos, covers)

    @override_settings(DEMO_MODE=True)
    def test_a_failed_thumbnail_aborts_instead_of_persisting_a_gap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Linking a cover whose social rendition failed would make the gap permanent."""

        def half_failed(original_path: str, config: t.Any) -> ThumbnailResult:
            return ThumbnailResult(thumbnails={}, failures={"logo_thumbnail": "boom"})

        monkeypatch.setattr(
            "events.management.commands.demo_video_helpers.artwork.generate_and_save_thumbnails", half_failed
        )

        with pytest.raises(RuntimeError, match="Demo artwork thumbnails failed"):
            _run()

        assert not Organization.objects.filter(slug__in=DEMO_ORG_SLUGS).exists()

    @override_settings(DEMO_MODE=True)
    def test_every_demo_account_signs_in_with_the_shared_password(self) -> None:
        _run()

        users = RevelUser.objects.filter(email__endswith=f"@{DEMO_EMAIL_DOMAIN}")
        assert users.count() == 29
        assert all(user.check_password("password123") and user.email_verified for user in users)

    @override_settings(DEMO_MODE=True)
    def test_a_failing_scenario_rolls_the_whole_seed_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One broken scenario must not leave a half-seeded demo world behind."""

        def boom() -> ScenarioSummary:
            raise RuntimeError("scenario exploded")

        monkeypatch.setattr(bootstrap_demo_video, "SCENARIOS", [SCENARIOS[0], boom])

        with pytest.raises(RuntimeError, match="scenario exploded"):
            _run()

        assert all(count == 0 for count in _demo_counts().values())

    @override_settings(DEMO_MODE=False)
    def test_refuses_to_run_outside_demo_mode(self) -> None:
        """The seed publishes public orgs whose password is printed, so production is off-limits."""
        with pytest.raises(CommandError, match="DEMO_MODE"):
            _run()

        assert not Organization.objects.filter(slug__in=DEMO_ORG_SLUGS).exists()
