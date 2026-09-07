# src/events/management/commands/demo_video_helpers/base.py
"""Shared state and idempotent upsert helpers for the demo-video seed.

Every helper here is safe to call repeatedly: rows are keyed on stable slugs,
emails, or ``(parent, order)`` pairs, so a second run refreshes the existing
demo world instead of duplicating it.
"""

import datetime as dt
import typing as t
from dataclasses import dataclass, field
from decimal import Decimal

from django.utils import timezone

from accounts.models import RevelUser
from events import models as events_models
from geo.models import City
from questionnaires import models as questionnaires_models

DEMO_PASSWORD = "password123"
DEMO_EMAIL_DOMAIN = "demovideo.example.com"

#: Every organization gets this tier from the ``Organization`` post-save signal.
DEFAULT_MEMBERSHIP_TIER_NAME = "General membership"

EvaluationStatus = questionnaires_models.QuestionnaireEvaluation.QuestionnaireEvaluationStatus


def demo_email(first_name: str, role: str) -> str:
    """Build the demo-video address for ``first_name`` in ``role``."""
    return f"{first_name.lower()}.{role}@{DEMO_EMAIL_DOMAIN}"


@dataclass
class DemoAccount:
    """A seeded demo login, as printed in the command's credential summary."""

    email: str
    name: str
    role: str


@dataclass
class ScenarioSummary:
    """What one scenario seeded, for the on-camera cheat sheet."""

    title: str
    org_slug: str
    event_paths: list[str] = field(default_factory=list)
    accounts: list[DemoAccount] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, user: RevelUser, role: str) -> None:
        """Record ``user`` in the credential table under ``role``."""
        self.accounts.append(DemoAccount(email=user.email, name=user.get_display_name(), role=role))


def at_local_hour(days_ahead: int, hour: int, minute: int = 0) -> dt.datetime:
    """A timezone-aware datetime ``days_ahead`` days from now, at local ``hour``:``minute``.

    Dates are always computed relative to *now* so a seed recorded against months
    ago still shows upcoming events the next time the command runs.
    """
    return (timezone.localtime() + dt.timedelta(days=days_ahead)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )


def vienna() -> City | None:
    """The Vienna city row, or ``None`` on a DB without the geo fixtures loaded."""
    return City.objects.filter(name="Vienna", country="Austria").first()


def upsert_user(first_name: str, last_name: str, role: str, *, pronouns: str = "") -> RevelUser:
    """Create or refresh a demo account, keyed on its ``<first>.<role>@`` address."""
    email = demo_email(first_name, role)
    user, _ = RevelUser.objects.update_or_create(
        username=email,
        defaults={
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "pronouns": pronouns,
            "email_verified": True,
            "is_active": True,
        },
    )
    if not user.check_password(DEMO_PASSWORD):
        user.set_password(DEMO_PASSWORD)
        user.save(update_fields=["password"])
    return user


def upsert_organization(
    *,
    name: str,
    slug: str,
    owner: RevelUser,
    description: str,
    address: str,
    contact_email: str,
) -> events_models.Organization:
    """Create or refresh a public demo organization, keyed on its slug."""
    organization, _ = events_models.Organization.objects.update_or_create(
        slug=slug,
        defaults={
            "name": name,
            "owner": owner,
            "description": description,
            "visibility": events_models.Organization.Visibility.PUBLIC,
            "city": vienna(),
            "address": address,
            "accept_membership_requests": True,
            "contact_email": contact_email,
            "contact_email_verified": True,
        },
    )
    return organization


def default_membership_tier(organization: events_models.Organization) -> events_models.MembershipTier:
    """The organization's default membership tier, creating it if it went missing."""
    tier, _ = events_models.MembershipTier.objects.get_or_create(
        organization=organization, name=DEFAULT_MEMBERSHIP_TIER_NAME
    )
    return tier


def upsert_membership(
    organization: events_models.Organization,
    user: RevelUser,
    tier: events_models.MembershipTier | None = None,
) -> events_models.OrganizationMember:
    """Give ``user`` an active membership of ``organization``."""
    member, _ = events_models.OrganizationMember.objects.update_or_create(
        organization=organization,
        user=user,
        defaults={
            "tier": tier or default_membership_tier(organization),
            "status": events_models.OrganizationMember.MembershipStatus.ACTIVE,
        },
    )
    return member


def upsert_event(
    *,
    organization: events_models.Organization,
    slug: str,
    name: str,
    description: str,
    address: str,
    start: dt.datetime,
    duration: dt.timedelta,
    **extra: t.Any,
) -> events_models.Event:
    """Create or refresh a demo event, keyed on ``(organization, slug)``.

    ``extra`` overrides any of the open/public defaults — pass ``requires_ticket``,
    ``event_type``, ``potluck_open``, and friends there.
    """
    defaults: dict[str, t.Any] = {
        "name": name,
        "description": description,
        "address": address,
        "city": vienna(),
        "start": start,
        "end": start + duration,
        "status": events_models.Event.EventStatus.OPEN,
        "visibility": events_models.Event.Visibility.PUBLIC,
        "event_type": events_models.Event.EventType.PUBLIC,
    }
    defaults.update(extra)
    event, _ = events_models.Event.objects.update_or_create(organization=organization, slug=slug, defaults=defaults)
    return event


def drop_default_ticket_tier(event: events_models.Event) -> None:
    """Remove the tier the event post-save signal auto-creates for ticketed events."""
    events_models.TicketTier.objects.filter(event=event, name=events_models.DEFAULT_TICKET_TIER_NAME).delete()


def upsert_ticket_tier(event: events_models.Event, name: str, **defaults: t.Any) -> events_models.TicketTier:
    """Create or refresh a ticket tier, keyed on ``(event, name)``."""
    tier, _ = events_models.TicketTier.objects.update_or_create(event=event, name=name, defaults=defaults)
    return tier


def upsert_rsvp(
    event: events_models.Event,
    user: RevelUser,
    status: str = events_models.EventRSVP.RsvpStatus.YES,
) -> events_models.EventRSVP:
    """Create or refresh an RSVP, keyed on ``(event, user)``."""
    rsvp, _ = events_models.EventRSVP.objects.update_or_create(event=event, user=user, defaults={"status": status})
    return rsvp


def upsert_invitation(
    event: events_models.Event,
    user: RevelUser,
    *,
    custom_message: str,
    waives_questionnaire: bool = False,
    waives_membership_required: bool = False,
) -> events_models.EventInvitation:
    """Create or refresh an event invitation, keyed on ``(event, user)``."""
    invitation, _ = events_models.EventInvitation.objects.update_or_create(
        event=event,
        user=user,
        defaults={
            "custom_message": custom_message,
            "waives_questionnaire": waives_questionnaire,
            "waives_membership_required": waives_membership_required,
        },
    )
    return invitation


def upsert_potluck_item(
    event: events_models.Event,
    *,
    name: str,
    item_type: str,
    quantity: str,
    note: str,
    assignee: RevelUser | None = None,
) -> events_models.PotluckItem:
    """Create or refresh a potluck item, keyed on ``(event, name)``.

    An item with no ``assignee`` is a host suggestion waiting for a volunteer.
    """
    item, _ = events_models.PotluckItem.objects.update_or_create(
        event=event,
        name=name,
        defaults={
            "item_type": item_type,
            "quantity": quantity,
            "note": note,
            "is_suggested": assignee is None,
            "assignee": assignee,
            "created_by": assignee or event.organization.owner,
        },
    )
    return item


def upsert_questionnaire(
    *,
    organization: events_models.Organization,
    name: str,
    description: str,
    events: list[events_models.Event],
) -> questionnaires_models.Questionnaire:
    """Create or refresh a published, manually-reviewed admission questionnaire.

    Keyed on ``(organization, questionnaire name)`` via the ``OrganizationQuestionnaire``
    link, so the name only has to be unique within the organization.
    """
    org_questionnaire = events_models.OrganizationQuestionnaire.objects.filter(
        organization=organization, questionnaire__name=name
    ).first()
    questionnaire = org_questionnaire.questionnaire if org_questionnaire else questionnaires_models.Questionnaire()
    questionnaire.name = name
    questionnaire.description = description
    questionnaire.status = questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED
    questionnaire.evaluation_mode = questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.MANUAL
    questionnaire.llm_backend = questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK
    questionnaire.min_score = Decimal("60.00")
    questionnaire.max_attempts = 3
    questionnaire.save()

    if org_questionnaire is None:
        org_questionnaire = events_models.OrganizationQuestionnaire.objects.create(
            organization=organization,
            questionnaire=questionnaire,
            questionnaire_type=events_models.OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
    org_questionnaire.events.set(events)
    return questionnaire


def upsert_section(
    questionnaire: questionnaires_models.Questionnaire, name: str, order: int = 1
) -> questionnaires_models.QuestionnaireSection:
    """Create or refresh a questionnaire section, keyed on ``(questionnaire, order)``."""
    section, _ = questionnaires_models.QuestionnaireSection.objects.update_or_create(
        questionnaire=questionnaire, order=order, defaults={"name": name}
    )
    return section


def upsert_free_text_question(
    section: questionnaires_models.QuestionnaireSection,
    question: str,
    *,
    order: int = 1,
    hint: str | None = None,
) -> questionnaires_models.FreeTextQuestion:
    """Create or refresh a mandatory free-text question, keyed on ``(questionnaire, order)``.

    Question text is a ``MarkdownField`` and is rewritten by the sanitizer on save, so
    the *order* — not the prose — is the stable key.
    """
    free_text_question, _ = questionnaires_models.FreeTextQuestion.objects.update_or_create(
        questionnaire=section.questionnaire,
        order=order,
        defaults={
            "section": section,
            "question": question,
            "hint": hint,
            "is_mandatory": True,
            "is_fatal": False,
            "positive_weight": Decimal("3.0"),
            "negative_weight": Decimal("0.0"),
        },
    )
    return free_text_question


def upsert_choice_question(
    section: questionnaires_models.QuestionnaireSection,
    question: str,
    options: list[str],
    *,
    order: int = 1,
) -> tuple[questionnaires_models.MultipleChoiceQuestion, list[questionnaires_models.MultipleChoiceOption]]:
    """Create or refresh a single-answer multiple-choice question and its options."""
    choice_question, _ = questionnaires_models.MultipleChoiceQuestion.objects.update_or_create(
        questionnaire=section.questionnaire,
        order=order,
        defaults={
            "section": section,
            "question": question,
            "allow_multiple_answers": False,
            "shuffle_options": False,
            "is_mandatory": True,
            "is_fatal": False,
            "positive_weight": Decimal("1.0"),
            "negative_weight": Decimal("0.0"),
        },
    )
    rows = []
    for index, option in enumerate(options, 1):
        row, _ = questionnaires_models.MultipleChoiceOption.objects.update_or_create(
            question=choice_question, order=index, defaults={"option": option, "is_correct": False}
        )
        rows.append(row)
    return choice_question, rows


def upsert_submission(
    *,
    user: RevelUser,
    event: events_models.Event,
    questionnaire: questionnaires_models.Questionnaire,
    free_text: dict[questionnaires_models.FreeTextQuestion, str],
    choices: dict[questionnaires_models.MultipleChoiceQuestion, questionnaires_models.MultipleChoiceOption]
    | None = None,
    submitted_days_ago: int,
    status: str = EvaluationStatus.PENDING_REVIEW,
    score: Decimal | None = None,
    comments: str = "",
) -> questionnaires_models.QuestionnaireSubmission:
    """Create or refresh one submitted questionnaire, its answers, and its evaluation.

    A ``PENDING_REVIEW`` evaluation is what puts an application on the organizer's
    review board, so pending submissions get a real evaluation row too.
    """
    submission = questionnaires_models.QuestionnaireSubmission.objects.filter(
        user=user, questionnaire=questionnaire
    ).first()
    if submission is None:
        submission = questionnaires_models.QuestionnaireSubmission(user=user, questionnaire=questionnaire)
    submission.status = questionnaires_models.QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    submission.submitted_at = at_local_hour(-submitted_days_ago, hour=11)
    submission.save()

    for question, answer in free_text.items():
        questionnaires_models.FreeTextAnswer.objects.update_or_create(
            submission=submission, question=question, defaults={"answer": answer}
        )
    for choice_question, option in (choices or {}).items():
        questionnaires_models.MultipleChoiceAnswer.objects.filter(
            submission=submission, question=choice_question
        ).exclude(option=option).delete()
        questionnaires_models.MultipleChoiceAnswer.objects.get_or_create(
            submission=submission, question=choice_question, option=option
        )

    questionnaires_models.QuestionnaireEvaluation.objects.update_or_create(
        submission=submission,
        defaults={"status": status, "score": score, "comments": comments or None},
    )
    events_models.EventQuestionnaireSubmission.objects.update_or_create(
        submission=submission,
        defaults={
            "user": user,
            "event": event,
            "questionnaire": questionnaire,
            "questionnaire_type": events_models.OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        },
    )
    return submission
