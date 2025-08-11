# src/events/management/commands/bootstrap_events.py

import logging
import typing as t
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from faker import Faker

from accounts.models import RevelUser
from events import models as events_models
from geo.models import City
from questionnaires import models as questionnaires_models

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Bootstrap example data for events, organizations, and questionnaires."

    def handle(self, *args: t.Any, **options: t.Any) -> None:  # pragma: no cover
        """Bootstrap example data."""
        # Orgs

        fake = Faker("de_AT")

        org_alpha_owner = RevelUser.objects.create_user(username="org-alpha-owner@example.com", password="password")

        org_alpha = events_models.Organization.objects.create(
            name="Organization Alpha",
            slug="organization-alpha",
            owner=org_alpha_owner,  # replace with proper user lookup
            visibility=events_models.Organization.Visibility.PUBLIC,
        )

        org_alpha_member = RevelUser.objects.create_user(username="org-alpha-member@example.com", password="password")
        org_alpha.members.add(org_alpha_member)

        org_alpha_staff_member = RevelUser.objects.create_user(
            username="org-alpha-staff-member@example.com", password="password"
        )
        org_alpha.staff_members.add(org_alpha_staff_member)

        random_user = RevelUser.objects.create_user(username="random-user@example.com", password="password")

        next_week = timezone.now() + timedelta(days=7)

        vienna = City.objects.get(name="Vienna", country="Austria")
        new_york = City.objects.filter(name="New York", country="United States").first()
        assert new_york is not None

        def fake_address() -> str:
            return " ".join(fake.address().split())

        # Event without questionnaire
        events_models.Event.objects.create(
            organization=org_alpha,
            name="Public Event no Ticket",
            slug="public-event",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=vienna,
            requires_ticket=False,
            start=next_week,
            end=next_week + timedelta(days=1),
            description=fake.text(),
            address=fake_address(),
        )

        events_models.Event.objects.create(
            organization=org_alpha,
            name="Public Event with Ticket",
            slug="public-event-with-ticket",
            event_type=events_models.Event.Types.PUBLIC,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            city=new_york,
            requires_ticket=True,
            start=next_week,
            end=next_week + timedelta(days=1),
            description=fake.text(),
            address=fake_address(),
        )

        events_models.Event.objects.create(
            organization=org_alpha,
            name="Private Event with Ticket",
            slug="private-event-with-ticket",
            event_type=events_models.Event.Types.PRIVATE,
            visibility=events_models.Event.Visibility.PRIVATE,
            status=events_models.Event.Status.OPEN,
            requires_ticket=True,
            city=vienna,
            start=next_week,
            end=next_week + timedelta(days=1),
            description=fake.text(),
            address=fake_address(),
        )

        events_models.Event.objects.create(
            organization=org_alpha,
            name="Public Event Members Only",
            slug="public-event-members-only",
            event_type=events_models.Event.Types.MEMBERS_ONLY,
            visibility=events_models.Event.Visibility.PUBLIC,
            status=events_models.Event.Status.OPEN,
            requires_ticket=False,
            city=new_york,
            start=next_week,
            end=next_week + timedelta(days=1),
            description=fake.text(),
            address=fake_address(),
        )

        events_models.Event.objects.create(
            organization=org_alpha,
            name="Members Only Event",
            slug="members-only-event",
            event_type=events_models.Event.Types.MEMBERS_ONLY,
            visibility=events_models.Event.Visibility.MEMBERS_ONLY,
            status=events_models.Event.Status.OPEN,
            requires_ticket=False,
            city=vienna,
            start=next_week,
            end=next_week + timedelta(days=1),
            description=fake.text(),
            address=fake_address(),
        )

        # Event with questionnaire
        event_with_questionnaire = events_models.Event.objects.create(
            organization=org_alpha,
            name="Private Event no Ticket with Questionnaire",
            slug="private-event",
            event_type=events_models.Event.Types.PRIVATE,
            visibility=events_models.Event.Visibility.PRIVATE,
            status=events_models.Event.Status.OPEN,
            requires_ticket=False,
            city=new_york,
            start=next_week,
            end=next_week + timedelta(days=1),
            description="An event reserved for Cool People.",
            address=fake_address(),
        )

        events_models.EventInvitation.objects.create(
            event=event_with_questionnaire,
            user=random_user,
            waives_questionnaire=False,
        )

        # Create questionnaire
        questionnaire = questionnaires_models.Questionnaire.objects.create(
            name="Beta Admission Questionnaire",
            status=questionnaires_models.Questionnaire.Status.PUBLISHED,
            evaluation_mode=questionnaires_models.Questionnaire.EvaluationMode.AUTOMATIC,
            shuffle_questions=True,
            llm_guidelines="Only cool people should be given a passing evaluation to this questionnaire.",
            max_attempts=2,
            can_retake_after=timedelta(minutes=5),
        )

        # Section
        section = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="General Section",
            order=1,
        )

        # Multiple Choice Question — mandatory and fatal
        mcq = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Do you agree to the Code of Conduct?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=True,
            order=1,
        )
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=mcq,
            option="Yes",
            is_correct=True,
            order=1,
        )
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=mcq,
            option="No",
            is_correct=False,
            order=2,
        )

        # Free Text Question — optional
        questionnaires_models.FreeTextQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Tell us why you want to join.",
            llm_guidelines="Evaluate clarity and relevance.",
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=False,
            order=2,
        )

        # Link Questionnaire to Event
        org_quest = events_models.OrganizationQuestionnaire.objects.create(
            organization=org_alpha,
            questionnaire=questionnaire,
        )
        org_quest.events.add(event_with_questionnaire)

        questionnaire = questionnaires_models.Questionnaire.objects.create(
            name="Beta Second Admission Questionnaire",
            status=questionnaires_models.Questionnaire.Status.PUBLISHED,
            evaluation_mode=questionnaires_models.Questionnaire.EvaluationMode.AUTOMATIC,
            shuffle_questions=True,
            max_attempts=2,
            can_retake_after=timedelta(minutes=5),
        )

        # Section
        section = questionnaires_models.QuestionnaireSection.objects.create(
            questionnaire=questionnaire,
            name="Specific Section",
            order=1,
        )

        # Multiple Choice Question — mandatory and fatal
        mcq = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Do you agree to the Code of Conduct?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=True,
            order=1,
        )
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=mcq,
            option="Yes",
            is_correct=True,
            order=1,
        )
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=mcq,
            option="No",
            is_correct=False,
            order=2,
        )

        mcq2 = questionnaires_models.MultipleChoiceQuestion.objects.create(
            questionnaire=questionnaire,
            section=section,
            question="Are you older than 18?",
            allow_multiple_answers=False,
            shuffle_options=False,
            positive_weight=1,
            negative_weight=0,
            is_fatal=True,
            is_mandatory=True,
            order=1,
        )
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=mcq2,
            option="Yes",
            is_correct=True,
            order=1,
        )
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=mcq2,
            option="No",
            is_correct=False,
            order=2,
        )

        org_quest = events_models.OrganizationQuestionnaire.objects.create(
            organization=org_alpha,
            questionnaire=questionnaire,
        )
        # org_quest.events.add(event_with_questionnaire)

        logger.info("Bootstrap complete: 2 orgs, 2 events, 1 questionnaire with 2 questions.")
