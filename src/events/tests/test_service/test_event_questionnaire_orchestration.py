"""Unit tests for orchestration helpers in event_questionnaire_service.

Covers the functions extracted out of `events/controllers/questionnaire.py`:

- create_org_questionnaire
- set_status
- replace_events
- replace_event_series
- start_submissions_export
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from events.models import Event, EventSeries, Organization, OrganizationQuestionnaire
from events.schema import OrganizationQuestionnaireCreateSchema
from events.service import event_questionnaire_service
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# Local fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def orchestration_org(django_user_model: type[RevelUser]) -> Organization:
    """Organization owning the questionnaires under test."""
    owner = django_user_model.objects.create_user(
        username="orchestration_owner",
        email="orchestration_owner@example.com",
        password="pass",
    )
    return Organization.objects.create(
        name="Orchestration Org",
        slug="orchestration-org",
        owner=owner,
    )


@pytest.fixture
def other_org(django_user_model: type[RevelUser]) -> Organization:
    """A different organization (used for ownership-violation tests)."""
    owner = django_user_model.objects.create_user(
        username="other_owner",
        email="other_owner@example.com",
        password="pass",
    )
    return Organization.objects.create(
        name="Other Org",
        slug="other-org",
        owner=owner,
    )


@pytest.fixture
def orchestration_event(orchestration_org: Organization) -> Event:
    """An event belonging to the orchestration org."""
    return Event.objects.create(
        organization=orchestration_org,
        name="Orchestration Event",
        slug="orchestration-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() - timedelta(days=2),
        end=timezone.now() - timedelta(days=1),
        requires_ticket=False,
    )


@pytest.fixture
def other_org_event(other_org: Organization) -> Event:
    """An event belonging to a different org."""
    return Event.objects.create(
        organization=other_org,
        name="Other Org Event",
        slug="other-org-event",
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        status="open",
        start=timezone.now() - timedelta(days=2),
        end=timezone.now() - timedelta(days=1),
        requires_ticket=False,
    )


@pytest.fixture
def orchestration_series(orchestration_org: Organization) -> EventSeries:
    """An event series belonging to the orchestration org."""
    return EventSeries.objects.create(
        organization=orchestration_org,
        name="Orchestration Series",
        slug="orchestration-series",
    )


@pytest.fixture
def other_org_series(other_org: Organization) -> EventSeries:
    """An event series belonging to a different org."""
    return EventSeries.objects.create(
        organization=other_org,
        name="Other Org Series",
        slug="other-org-series",
    )


@pytest.fixture
def orchestration_questionnaire() -> Questionnaire:
    """A questionnaire used as the underlying questionnaire for status/M2M tests."""
    return Questionnaire.objects.create(
        name="Orchestration Questionnaire",
        status=Questionnaire.QuestionnaireStatus.DRAFT,
    )


@pytest.fixture
def orchestration_org_questionnaire(
    orchestration_org: Organization,
    orchestration_questionnaire: Questionnaire,
) -> OrganizationQuestionnaire:
    """OrganizationQuestionnaire bound to the orchestration org."""
    return OrganizationQuestionnaire.objects.create(
        organization=orchestration_org,
        questionnaire=orchestration_questionnaire,
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    )


# --------------------------------------------------------------------------- #
# create_org_questionnaire
# --------------------------------------------------------------------------- #


class TestCreateOrgQuestionnaire:
    """Tests for event_questionnaire_service.create_org_questionnaire."""

    def test_creates_questionnaire_and_wrapper(self, orchestration_org: Organization) -> None:
        """A valid payload creates both the Questionnaire and OrganizationQuestionnaire."""
        payload = OrganizationQuestionnaireCreateSchema(
            name="Created Questionnaire",
            min_score=Decimal("0.0"),
            evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )

        result = event_questionnaire_service.create_org_questionnaire(orchestration_org, payload)

        assert result.organization_id == orchestration_org.id
        assert result.questionnaire.name == "Created Questionnaire"
        assert result.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.ADMISSION
        assert result.max_submission_age is None
        assert result.requires_evaluation is True

    def test_propagates_optional_fields(self, orchestration_org: Organization) -> None:
        """Optional wrapper fields are passed through to the OrganizationQuestionnaire."""
        payload = OrganizationQuestionnaireCreateSchema(
            name="Wrapper Fields",
            min_score=Decimal("0.0"),
            evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
            max_submission_age=timedelta(hours=1),
            members_exempt=True,
            per_event=True,
            requires_evaluation=False,
        )

        result = event_questionnaire_service.create_org_questionnaire(orchestration_org, payload)

        assert result.max_submission_age == timedelta(hours=1)
        assert result.members_exempt is True
        assert result.per_event is True
        assert result.requires_evaluation is False
        assert result.questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK

    def test_rejects_feedback_with_evaluation(self, orchestration_org: Organization) -> None:
        """Feedback questionnaires that require evaluation are rejected with HTTP 400."""
        payload = OrganizationQuestionnaireCreateSchema(
            name="Bad Feedback",
            min_score=Decimal("0.0"),
            evaluation_mode=Questionnaire.QuestionnaireEvaluationMode.MANUAL,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
            requires_evaluation=True,
        )

        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.create_org_questionnaire(orchestration_org, payload)

        assert exc_info.value.status_code == 400
        # No partial state was persisted.
        assert not OrganizationQuestionnaire.objects.filter(organization=orchestration_org).exists()
        assert not Questionnaire.objects.filter(name="Bad Feedback").exists()


# --------------------------------------------------------------------------- #
# set_status
# --------------------------------------------------------------------------- #


class TestSetStatus:
    """Tests for event_questionnaire_service.set_status."""

    def test_updates_underlying_questionnaire_status(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """Calling set_status updates the nested Questionnaire status."""
        assert orchestration_org_questionnaire.questionnaire.status == Questionnaire.QuestionnaireStatus.DRAFT

        result = event_questionnaire_service.set_status(
            orchestration_org_questionnaire, Questionnaire.QuestionnaireStatus.READY
        )

        assert result.questionnaire.status == Questionnaire.QuestionnaireStatus.READY
        # And the change was persisted.
        result.questionnaire.refresh_from_db()
        assert result.questionnaire.status == Questionnaire.QuestionnaireStatus.READY

    def test_does_not_touch_other_fields(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """set_status persists only the status field (via update_fields)."""
        original_name = orchestration_org_questionnaire.questionnaire.name

        # Mutate name in memory only — set_status must not persist it.
        orchestration_org_questionnaire.questionnaire.name = "Should not be saved"

        event_questionnaire_service.set_status(
            orchestration_org_questionnaire, Questionnaire.QuestionnaireStatus.PUBLISHED
        )

        reloaded = Questionnaire.objects.get(pk=orchestration_org_questionnaire.questionnaire_id)
        assert reloaded.name == original_name
        assert reloaded.status == Questionnaire.QuestionnaireStatus.PUBLISHED


# --------------------------------------------------------------------------- #
# replace_events
# --------------------------------------------------------------------------- #


class TestReplaceEvents:
    """Tests for event_questionnaire_service.replace_events."""

    def test_assigns_org_owned_events(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_event: Event,
    ) -> None:
        """Events that belong to the org are assigned successfully."""
        result = event_questionnaire_service.replace_events(orchestration_org_questionnaire, [orchestration_event.id])
        assert list(result.events.values_list("id", flat=True)) == [orchestration_event.id]

    def test_empty_list_clears_events(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_event: Event,
    ) -> None:
        """An empty list clears the existing assignment."""
        orchestration_org_questionnaire.events.add(orchestration_event)
        assert orchestration_org_questionnaire.events.exists()

        event_questionnaire_service.replace_events(orchestration_org_questionnaire, [])

        assert not orchestration_org_questionnaire.events.exists()

    def test_rejects_event_owned_by_other_org(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        other_org_event: Event,
    ) -> None:
        """Events belonging to another organization are rejected with HTTP 400."""
        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.replace_events(orchestration_org_questionnaire, [other_org_event.id])
        assert exc_info.value.status_code == 400
        assert not orchestration_org_questionnaire.events.exists()

    def test_rejects_unknown_event_id(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """An unknown event id raises HTTP 400 (no partial assignment)."""
        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.replace_events(orchestration_org_questionnaire, [uuid4()])
        assert exc_info.value.status_code == 400
        assert not orchestration_org_questionnaire.events.exists()

    def test_mixed_valid_and_invalid_event_ids(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_event: Event,
        other_org_event: Event,
    ) -> None:
        """If any id is invalid, the entire call is rejected and no events are assigned."""
        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.replace_events(
                orchestration_org_questionnaire, [orchestration_event.id, other_org_event.id]
            )
        assert exc_info.value.status_code == 400
        assert not orchestration_org_questionnaire.events.exists()


# --------------------------------------------------------------------------- #
# replace_event_series
# --------------------------------------------------------------------------- #


class TestReplaceEventSeries:
    """Tests for event_questionnaire_service.replace_event_series."""

    def test_assigns_org_owned_series(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_series: EventSeries,
    ) -> None:
        """Series that belong to the org are assigned successfully."""
        result = event_questionnaire_service.replace_event_series(
            orchestration_org_questionnaire, [orchestration_series.id]
        )
        assert list(result.event_series.values_list("id", flat=True)) == [orchestration_series.id]

    def test_rejects_series_owned_by_other_org(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        other_org_series: EventSeries,
    ) -> None:
        """Series belonging to another organization are rejected with HTTP 400."""
        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.replace_event_series(orchestration_org_questionnaire, [other_org_series.id])
        assert exc_info.value.status_code == 400
        assert not orchestration_org_questionnaire.event_series.exists()

    def test_rejects_unknown_series_id(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
    ) -> None:
        """An unknown series id raises HTTP 400."""
        with pytest.raises(HttpError) as exc_info:
            event_questionnaire_service.replace_event_series(orchestration_org_questionnaire, [uuid4()])
        assert exc_info.value.status_code == 400
        assert not orchestration_org_questionnaire.event_series.exists()


# --------------------------------------------------------------------------- #
# start_submissions_export
# --------------------------------------------------------------------------- #


class TestStartSubmissionsExport:
    """Tests for event_questionnaire_service.start_submissions_export."""

    def test_creates_export_and_dispatches_task(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_org: Organization,
    ) -> None:
        """A FileExport is created and the generation task is enqueued."""
        from common.models import FileExport

        with patch("events.tasks.generate_questionnaire_export_task.delay") as delay_mock:
            export = event_questionnaire_service.start_submissions_export(
                orchestration_org_questionnaire,
                requested_by=orchestration_org.owner,
            )

        assert isinstance(export, FileExport)
        assert export.requested_by_id == orchestration_org.owner.id
        assert export.export_type == FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS
        assert export.parameters == {
            "questionnaire_id": str(orchestration_org_questionnaire.questionnaire_id),
        }
        delay_mock.assert_called_once_with(str(export.id))

    def test_propagates_event_filter(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_org: Organization,
        orchestration_event: Event,
    ) -> None:
        """An event_id filter is stored on the FileExport parameters."""
        with patch("events.tasks.generate_questionnaire_export_task.delay"):
            export = event_questionnaire_service.start_submissions_export(
                orchestration_org_questionnaire,
                requested_by=orchestration_org.owner,
                event_id=orchestration_event.id,
            )

        assert export.parameters["event_id"] == str(orchestration_event.id)
        assert "event_series_id" not in export.parameters

    def test_propagates_event_series_filter(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_org: Organization,
        orchestration_series: EventSeries,
    ) -> None:
        """An event_series_id filter is stored on the FileExport parameters."""
        with patch("events.tasks.generate_questionnaire_export_task.delay"):
            export = event_questionnaire_service.start_submissions_export(
                orchestration_org_questionnaire,
                requested_by=orchestration_org.owner,
                event_series_id=orchestration_series.id,
            )

        assert export.parameters["event_series_id"] == str(orchestration_series.id)
        assert "event_id" not in export.parameters

    def test_rejects_both_event_and_series_filters(
        self,
        orchestration_org_questionnaire: OrganizationQuestionnaire,
        orchestration_org: Organization,
        orchestration_event: Event,
        orchestration_series: EventSeries,
    ) -> None:
        """Supplying both filters raises HTTP 400 and does not enqueue an export."""
        from common.models import FileExport

        with patch("events.tasks.generate_questionnaire_export_task.delay") as delay_mock:
            with pytest.raises(HttpError) as exc_info:
                event_questionnaire_service.start_submissions_export(
                    orchestration_org_questionnaire,
                    requested_by=orchestration_org.owner,
                    event_id=orchestration_event.id,
                    event_series_id=orchestration_series.id,
                )

        assert exc_info.value.status_code == 400
        delay_mock.assert_not_called()
        assert not FileExport.objects.filter(export_type=FileExport.ExportType.QUESTIONNAIRE_SUBMISSIONS).exists()
