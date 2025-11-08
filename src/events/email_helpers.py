"""Email notification helpers for events tasks.

This module contains helper functions for email rendering, attachment generation,
and context building used by the notification email tasks.
"""

import typing as t
from collections.abc import Callable

from django.db.models import Model

from accounts.models import RevelUser
from events.models import Event, Organization, Payment, PotluckItem, Ticket
from events.utils import create_ticket_pdf
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission

# Type alias for model fetcher functions
ModelFetcher = Callable[[str], Model]


# Registry mapping context keys to their fetcher functions
# Format: {context_key_suffix: (context_name, fetcher_lambda)}
CONTEXT_FETCHERS: dict[str, tuple[str, ModelFetcher]] = {
    "user_id": ("user", lambda pk: RevelUser.objects.get(pk=pk)),
    "event_id": ("event", lambda pk: Event.objects.select_related("organization", "city").get(pk=pk)),
    "organization_id": ("organization", lambda pk: Organization.objects.get(pk=pk)),
    "ticket_id": ("ticket", lambda pk: Ticket.objects.select_related("event", "user", "tier").get(pk=pk)),
    "payment_id": ("payment", lambda pk: Payment.objects.select_related("user", "ticket__event").get(pk=pk)),
    "potluck_item_id": (
        "potluck_item",
        lambda pk: PotluckItem.objects.select_related("event__organization", "assignee").get(pk=pk),
    ),
    "submission_id": (
        "submission",
        lambda pk: QuestionnaireSubmission.objects.select_related("questionnaire__event__organization", "user").get(
            pk=pk
        ),
    ),
    "evaluation_id": (
        "evaluation",
        lambda pk: QuestionnaireEvaluation.objects.select_related(
            "submission__questionnaire__event__organization", "submission__user", "evaluator"
        ).get(pk=pk),
    ),
    "changed_by_user_id": ("changed_by", lambda pk: RevelUser.objects.get(pk=pk)),
    "ticket_holder_id": ("ticket_holder", lambda pk: RevelUser.objects.get(pk=pk)),
}


def generate_attachment_content(attachment_spec: dict[str, str]) -> bytes:
    """Generate attachment content based on specification.

    Args:
        attachment_spec: Dict with 'type' and type-specific fields:
            - ticket_pdf: requires 'ticket_id'
            - event_ics: requires 'event_id'

    Returns:
        Attachment content as bytes

    Raises:
        ValueError: If attachment type is unknown
        Exception: If attachment generation fails (e.g., ticket/event not found)
    """
    att_type = attachment_spec["type"]

    if att_type == "ticket_pdf":
        ticket = Ticket.objects.select_related("event", "user", "tier").get(pk=attachment_spec["ticket_id"])
        return create_ticket_pdf(ticket)

    elif att_type == "event_ics":
        event = Event.objects.select_related("city").get(pk=attachment_spec["event_id"])
        return event.ics()

    raise ValueError(f"Unknown attachment type: {att_type}")


def build_email_context(context_ids: dict[str, t.Any]) -> dict[str, t.Any]:
    """Build email template context by fetching objects from IDs.

    Uses a registry-based approach to map context ID keys to their corresponding
    model fetchers. This avoids the if/elif spaghetti and makes it easy to add
    new context types.

    Args:
        context_ids: Dict with model IDs and raw context values

    Returns:
        Template context dict with fetched model instances and raw values
    """
    context: dict[str, t.Any] = {}

    # Fetch models using registry
    for key, pk in context_ids.items():
        if key in CONTEXT_FETCHERS:
            context_name, fetcher = CONTEXT_FETCHERS[key]
            obj = fetcher(pk)
            context[context_name] = obj

            # Handle derived context for specific models
            if key == "submission_id":
                context["questionnaire"] = obj.questionnaire  # type: ignore[attr-defined]
                context["submitter"] = obj.user  # type: ignore[attr-defined]

        # Non-ID fields are passed through as-is
        elif not key.endswith("_id"):
            context[key] = pk

    return context
