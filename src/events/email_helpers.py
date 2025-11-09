"""Email notification helpers for events tasks.

This module contains helper functions used by notification email tasks to:
1. Build template context from model IDs (avoiding pickling Django models in Celery tasks)
2. Generate email attachments (PDFs, ICS files)

Architecture pattern:
- Parent tasks (notify_*) dispatch child tasks using Celery groups for asynchronous execution
- Child tasks (send_notification_email) handle individual emails with retry logic
- Helpers here abstract common operations to maintain DRY principles

Registry pattern:
CONTEXT_FETCHERS maps context key suffixes (e.g., "user_id") to (context_name, fetcher) tuples.
This allows adding new context types without modifying build_email_context().

Example usage:
    # In a notification task
    context_ids = {
        "user_id": str(user.id),
        "event_id": str(event.id),
    }

    # In send_notification_email task
    context = build_email_context(context_ids)
    # context now contains: {"user": <User obj>, "event": <Event obj>}
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
        lambda pk: QuestionnaireSubmission.objects.select_related("questionnaire", "user").get(pk=pk),
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

    This function intentionally fails loudly on errors. If an attachment cannot be generated
    (e.g., ticket not found, PDF generation fails), the exception propagates to the caller,
    which will trigger email send retry logic.

    Args:
        attachment_spec: Dict with 'type' and type-specific fields:
            - ticket_pdf: requires 'ticket_id' (UUID as string)
            - event_ics: requires 'event_id' (UUID as string)

    Returns:
        Attachment content as bytes

    Raises:
        KeyError: If required fields are missing from attachment_spec
        ValueError: If attachment type is unknown
        Ticket.DoesNotExist: If ticket_id references non-existent ticket
        Event.DoesNotExist: If event_id references non-existent event
        RuntimeError: If PDF/ICS generation fails due to internal errors
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

    Note on derived context:
    For submission_id, this function adds derived context (questionnaire, submitter)
    from the submission object. This special-casing exists because submission emails
    need these values. See the registry implementation for details.

    Args:
        context_ids: Dict with model IDs (str) and raw context values (Any).
            Keys ending in '_id' are treated as model IDs and fetched from database.
            Other keys are passed through as-is.

    Returns:
        Template context dict with fetched model instances and raw values

    Raises:
        ObjectDoesNotExist: If any ID references a non-existent object
        ValueError: If ID format is invalid (e.g., invalid UUID)
    """
    context: dict[str, t.Any] = {}

    # Fetch models using registry
    for key, pk in context_ids.items():
        if key in CONTEXT_FETCHERS:
            context_name, fetcher = CONTEXT_FETCHERS[key]
            obj = fetcher(pk)
            context[context_name] = obj

            # Handle derived context for specific models
            # NOTE: This special-casing breaks the registry abstraction slightly, but is acceptable
            # because: (1) submission emails genuinely need both questionnaire and submitter in context,
            # (2) the alternative (nested fetchers or separate registry) adds complexity for one case,
            # (3) this is documented in the function docstring.
            # Type ignores are safe here because we know obj is a QuestionnaireSubmission when key == "submission_id"
            if key == "submission_id":
                context["questionnaire"] = obj.questionnaire  # type: ignore[attr-defined]
                context["submitter"] = obj.user  # type: ignore[attr-defined]

        # Non-ID fields are passed through as-is
        elif not key.endswith("_id"):
            context[key] = pk

    return context
