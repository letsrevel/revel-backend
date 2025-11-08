import typing as t
from collections import Counter
from uuid import UUID

import structlog
from celery import group, shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management import call_command
from django.db import transaction
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from common.models import EmailLog, SiteSettings
from common.tasks import send_email, to_safe_email_address
from events.email_helpers import build_email_context, generate_attachment_content
from events.service import update_db_instance

from .models import (
    AttendeeVisibilityFlag,
    Event,
    EventRSVP,
    OrganizationQuestionnaire,
    Payment,
    PotluckItem,
    Ticket,
    TicketTier,
)
from .service.notification_service import (
    NotificationType,
    get_eligible_users_for_event_notification,
    get_organization_staff_and_owners,
    log_notification_attempt,
    should_notify_user_for_questionnaire,
)

logger = structlog.get_logger(__name__)


# ==== Core Email Sending Task ====


@shared_task(bind=True, max_retries=3)
def send_notification_email(
    self: t.Any,
    recipient_email: str,
    subject: str,
    template_txt: str,
    template_html: str | None,
    context_ids: dict[str, t.Any],
    attachments: list[dict[str, str]] | None = None,
    user_id: str | None = None,
    notification_type: str | None = None,
    event_id: str | None = None,
) -> dict[str, t.Any]:
    """Send a single notification email with automatic retry logic.

    This task handles sending one email to one recipient. It automatically retries
    up to 3 times with exponential backoff (delays: 1s, 2s, 4s) on failure.
    Each email is isolated - one failure won't affect others.

    Note: We use manual retry with self.retry() instead of autoretry_for because
    we need custom behavior on final failure (logging to NotificationAttempt with
    success=False). Using autoretry_for would require a custom task class with
    on_failure() override, which is more complex than this approach.

    Args:
        self: the task itself.
        recipient_email: Email address to send to
        subject: Email subject line
        template_txt: Path to text template (e.g., 'events/emails/event_open.txt')
        template_html: Path to HTML template (optional, e.g., 'events/emails/event_open.html')
        context_ids: Dict of model IDs to fetch and pass to template:
            - user_id: User ID (optional)
            - event_id: Event ID (optional)
            - organization_id: Organization ID (optional)
            - ticket_id: Ticket ID (optional)
            - potluck_item_id: Potluck item ID (optional)
            - submission_id: Questionnaire submission ID (optional)
            - evaluation_id: Questionnaire evaluation ID (optional)
            - changed_by_user_id: User who made the change (optional)
            - ticket_holder_id: Ticket holder user ID (optional)
            Plus any other context data as raw values
        attachments: List of attachment specs, e.g.:
            [{'type': 'ticket_pdf', 'ticket_id': '...', 'filename': 'ticket.pdf', 'content_type': 'application/pdf'}]
            [{'type': 'event_ics', 'event_id': '...', 'filename': 'event.ics', 'content_type': 'text/calendar'}]
        user_id: ID of recipient user for notification logging (optional)
        notification_type: Notification type for logging (optional, string value of NotificationType enum)
        event_id: Event ID for notification logging (optional)

    Returns:
        Dict with:
            - success (bool): Whether email was sent successfully
            - recipient (str): Email address
            - user_id (str|None): User ID if provided
            - error (str): Error message if failed (only on final failure)

    Raises:
        self.retry: On failure, automatically retries with exponential backoff
    """
    try:
        # Build template context by fetching objects from IDs
        context = build_email_context(context_ids)

        # Render templates
        body = render_to_string(template_txt, context)
        html_body = render_to_string(template_html, context) if template_html else None

        # Get safe recipient (respects debug mode email routing)
        site_settings = SiteSettings.get_solo()
        recipient = to_safe_email_address(recipient_email, site_settings=site_settings)

        # Build email message
        email_msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            bcc=[recipient],
        )

        if html_body:
            email_msg.attach_alternative(html_body, "text/html")

        # Generate and attach files (CRITICAL: fails loudly if generation fails)
        if attachments:
            for att_spec in attachments:
                content = generate_attachment_content(att_spec)
                email_msg.attach(
                    att_spec.get("filename", "attachment"),
                    content,
                    att_spec.get("content_type", "application/octet-stream"),
                )

        # Send email (fail_silently=False means it raises on SMTP errors)
        email_msg.send(fail_silently=False)

        # Log the email for auditing
        email_log = EmailLog(to=recipient, subject=subject)
        email_log.set_body(body=body)
        if html_body:
            email_log.set_html(html_body=html_body)
        email_log.save()

        # Log notification attempt (success)
        if user_id and notification_type:
            user = RevelUser.objects.get(pk=user_id)
            event = Event.objects.get(pk=event_id) if event_id else None
            # Convert string back to enum for logging
            notif_type_enum = NotificationType(notification_type)
            log_notification_attempt(user, notif_type_enum, event=event, success=True)

        logger.info("notification_email_sent", recipient=recipient_email, subject=subject)

        return {
            "success": True,
            "recipient": recipient_email,
            "user_id": user_id,
        }

    except Exception as e:
        # Check if we've exhausted retries
        # Pattern: self.request.retries tracks current retry count (0-indexed)
        # If retries >= max_retries, this is the final attempt
        if self.request.retries >= self.max_retries:
            # Final failure - log permanently and return failure result
            logger.error(
                "notification_email_failed_permanently",
                recipient=recipient_email,
                subject=subject,
                attempts=self.max_retries + 1,
                error=str(e),
                exc_info=True,
            )

            # Log notification attempt (failure) for tracking
            if user_id and notification_type:
                try:
                    user = RevelUser.objects.get(pk=user_id)
                    event = Event.objects.get(pk=event_id) if event_id else None
                    # Convert string back to enum for logging
                    notif_type_enum = NotificationType(notification_type)
                    log_notification_attempt(user, notif_type_enum, event=event, success=False)
                except Exception as log_error:
                    logger.error("failed_to_log_notification_attempt", error=str(log_error))

            # Return failure result (doesn't raise - task completes)
            return {
                "success": False,
                "recipient": recipient_email,
                "user_id": user_id,
                "error": str(e),
            }

        # Retry with exponential backoff: 2^0=1s, 2^1=2s, 2^2=4s
        countdown = 2**self.request.retries
        logger.warning(
            "notification_email_failed_retrying",
            recipient=recipient_email,
            subject=subject,
            attempt=self.request.retries + 1,
            max_attempts=self.max_retries + 1,
            retry_in_seconds=countdown,
            error=str(e),
        )
        # Raises Retry exception - task will be retried after countdown
        raise self.retry(exc=e, countdown=countdown)


@shared_task
def build_attendee_visibility_flags(event_id: str) -> None:
    """A task that builds flags for attendee visibility events."""
    from .service.user_preferences_service import resolve_visibility

    event = Event.objects.with_organization().get(pk=event_id)

    # Users attending the event

    attendees_q = Q(tickets__event=event, tickets__status=Ticket.Status.ACTIVE) | Q(
        rsvps__event=event, rsvps__status=EventRSVP.Status.YES
    )

    attendees = RevelUser.objects.filter(attendees_q).distinct()

    update_db_instance(event, attendee_count=attendees.count())

    # Users invited or attending = potential viewers
    viewers = RevelUser.objects.filter(Q(invitations__event=event) | attendees_q).distinct()

    flags = []

    organization = event.organization
    owner_id = organization.owner_id
    staff_ids = {sm.id for sm in organization.staff_members.all()}

    with transaction.atomic():
        AttendeeVisibilityFlag.objects.filter(event=event).delete()
        for viewer in viewers:
            for target in attendees:
                visible = resolve_visibility(viewer, target, event, owner_id, staff_ids)
                flags.append(
                    AttendeeVisibilityFlag(
                        user=viewer,
                        target=target,
                        event=event,
                        is_visible=visible,
                    )
                )

        AttendeeVisibilityFlag.objects.bulk_create(flags)


@shared_task
def send_payment_confirmation_email(payment_id: str) -> dict[str, int]:
    """Send payment confirmation email to the user with PDF and ICS attachments.

    Dispatches email asynchronously with automatic retry on failure.

    Args:
        payment_id: The ID of the payment

    Returns:
        Dict with task dispatch info
    """
    payment = Payment.objects.select_related(
        "user", "ticket__event__organization", "ticket__event__city", "ticket__tier"
    ).get(pk=payment_id)
    user = payment.user
    ticket = payment.ticket
    event = ticket.event

    subject = f"Your Ticket for {event.name}"

    # Dispatch email task
    send_notification_email.delay(
        recipient_email=user.email,
        subject=subject,
        template_txt="events/emails/payment_confirmation.txt",
        template_html="events/emails/payment_confirmation.html",
        context_ids={
            "user_id": str(user.id),
            "ticket_id": str(ticket.id),
            "payment_id": str(payment.id),
        },
        attachments=[
            {
                "type": "ticket_pdf",
                "ticket_id": str(ticket.id),
                "filename": "ticket.pdf",
                "content_type": "application/pdf",
            },
            {
                "type": "event_ics",
                "event_id": str(event.id),
                "filename": "invite.ics",
                "content_type": "text/calendar",
            },
        ],
        user_id=str(user.id),
        notification_type=None,  # Payment confirmations don't use NotificationType
        event_id=None,
    )

    logger.info("payment_confirmation_dispatched", payment_id=payment_id, user_email=user.email)

    return {"dispatched": 1}


@shared_task(name="events.cleanup_expired_payments")
def cleanup_expired_payments() -> int:
    """Finds and deletes expired payments that are still in a 'pending' state.

    Releases their associated ticket reservation by decrementing the tier's
    quantity_sold counter.
    This task is idempotent and safe to run periodically.
    """
    # Find payments for tickets that are still pending and whose Stripe session has expired.
    expired_payments_qs = Payment.objects.filter(
        status=Payment.Status.PENDING, expires_at__lt=timezone.now()
    ).select_related("ticket", "ticket__tier")

    if not expired_payments_qs.exists():
        return 0

    # Collect IDs and tier counts before the transaction to avoid holding locks for too long
    payment_ids_to_delete = list(expired_payments_qs.values_list("id", flat=True))
    ticket_ids_to_delete = list(expired_payments_qs.values_list("ticket_id", flat=True))
    tickets_to_release_by_tier: Counter[UUID] = Counter(
        expired_payments_qs.filter(ticket__tier_id__isnull=False).values_list("ticket__tier_id", flat=True)
    )

    logger.info(
        f"Found {len(payment_ids_to_delete)} expired payments to clean up "
        f"across {len(tickets_to_release_by_tier)} tiers."
    )

    with transaction.atomic():
        # Atomically decrement the quantity_sold for each affected tier.
        for tier_id, count_to_release in tickets_to_release_by_tier.items():
            TicketTier.objects.select_for_update().filter(pk=tier_id).update(
                quantity_sold=F("quantity_sold") - count_to_release
            )

        # Delete payments first due to PROTECT constraint on Ticket
        Payment.objects.filter(pk__in=payment_ids_to_delete).delete()

        # Now delete the associated pending tickets
        Ticket.objects.filter(pk__in=ticket_ids_to_delete, status=Ticket.Status.PENDING).delete()

    logger.info(f"Successfully cleaned up {len(payment_ids_to_delete)} expired payments.")
    return len(payment_ids_to_delete)


@shared_task
def notify_event_open(event_id: str) -> dict[str, int]:
    """Send notifications when an event is opened.

    Dispatches emails in parallel. Each email is sent independently with
    automatic retry logic. Check logs and notification tracking for individual failures.

    Args:
        event_id: The ID of the event that was opened

    Returns:
        Dictionary with dispatch count: {'dispatched': N}
    """
    event = Event.objects.select_related("organization").get(pk=event_id)
    eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

    # Build email tasks for parallel execution
    email_tasks = []
    for user in eligible_users:
        task = send_notification_email.s(
            recipient_email=user.email,
            subject=f"New Event: {event.name}",
            template_txt="events/emails/event_open.txt",
            template_html="events/emails/event_open.html",
            context_ids={
                "user_id": str(user.id),
                "event_id": str(event.id),
                "organization_id": str(event.organization.id),
            },
            attachments=[
                {
                    "type": "event_ics",
                    "event_id": str(event.id),
                    "filename": "event.ics",
                    "content_type": "text/calendar",
                }
            ],
            user_id=str(user.id),
            notification_type=NotificationType.EVENT_OPEN.value,
            event_id=str(event.id),
        )
        email_tasks.append(task)

    # Dispatch all emails in parallel
    if email_tasks:
        job = group(email_tasks)
        job.apply_async()

    logger.info(
        "event_open_notifications_dispatched",
        event_id=event_id,
        event_name=event.name,
        count=len(email_tasks),
    )

    return {"dispatched": len(email_tasks)}


@shared_task
def notify_potluck_item_update(
    potluck_item_id: str, action: str, changed_by_user_id: str | None = None
) -> dict[str, int]:
    """Send notifications for potluck item updates.

    Dispatches emails in parallel. Each email is sent independently with
    automatic retry logic. Check logs and notification tracking for individual failures.

    Args:
        potluck_item_id: The ID of the potluck item that was updated
        action: The action performed ('created', 'deleted', 'assigned', 'unassigned')
        changed_by_user_id: ID of user who made the change (optional)

    Returns:
        Dictionary with dispatch and skip counts: {'dispatched': N, 'skipped': M}

    Raises:
        PotluckItem.DoesNotExist: If potluck item not found
    """
    potluck_item = PotluckItem.objects.select_related("event__organization", "assignee").get(pk=potluck_item_id)
    event = potluck_item.event

    # Get eligible users (including staff and owners)
    eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)
    staff_and_owners = get_organization_staff_and_owners(event.organization)

    # Combine and deduplicate using IDs to avoid queryset combination issues
    eligible_user_ids = set(eligible_users.values_list("id", flat=True))
    staff_and_owner_ids = set(staff_and_owners.values_list("id", flat=True))
    all_user_ids = eligible_user_ids | staff_and_owner_ids
    all_users = RevelUser.objects.filter(id__in=all_user_ids)

    # Build email tasks for parallel execution
    email_tasks = []
    skipped = 0

    for user in all_users:
        # Skip self-notification
        if changed_by_user_id and str(user.id) == changed_by_user_id:
            skipped += 1
            continue

        context_ids = {
            "user_id": str(user.id),
            "event_id": str(event.id),
            "potluck_item_id": str(potluck_item.id),
            "action": action,
        }

        # Add changed_by_user_id if present
        if changed_by_user_id:
            context_ids["changed_by_user_id"] = changed_by_user_id

        task = send_notification_email.s(
            recipient_email=user.email,
            subject=f"Potluck Update: {event.name}",
            template_txt="events/emails/potluck_update.txt",
            template_html="events/emails/potluck_update.html",
            context_ids=context_ids,
            attachments=None,
            user_id=str(user.id),
            notification_type=NotificationType.POTLUCK_UPDATE.value,
            event_id=str(event.id),
        )
        email_tasks.append(task)

    # Dispatch all emails in parallel
    if email_tasks:
        job = group(email_tasks)
        job.apply_async()

    logger.info(
        "potluck_update_notifications_dispatched",
        potluck_item_id=potluck_item_id,
        event_name=event.name,
        action=action,
        dispatched=len(email_tasks),
        skipped=skipped,
    )

    return {"dispatched": len(email_tasks), "skipped": skipped}


@shared_task
def notify_ticket_update(
    ticket_id: str, action: str, include_pdf: bool = True, include_ics: bool = True
) -> dict[str, int]:
    """Send notifications for ticket updates.

    Dispatches emails in parallel. Each email is sent independently with
    automatic retry logic. Check logs and notification tracking for individual failures.

    Args:
        ticket_id: The ID of the ticket that was updated
        action: The action performed (e.g., 'created', 'activated', 'payment_pending')
        include_pdf: Whether to include ticket PDF attachment (only for ACTIVE tickets)
        include_ics: Whether to include event ICS attachment

    Returns:
        Dictionary with dispatch count: {'dispatched': N}

    Raises:
        Ticket.DoesNotExist: If ticket not found
    """
    ticket = Ticket.objects.select_related("user", "event__organization", "tier").get(pk=ticket_id)

    user = ticket.user
    event = ticket.event

    email_tasks = []

    # Build attachments list
    attachments = []
    if include_pdf and ticket.status == Ticket.Status.ACTIVE:
        attachments.append(
            {
                "type": "ticket_pdf",
                "ticket_id": str(ticket.id),
                "filename": "ticket.pdf",
                "content_type": "application/pdf",
            }
        )
    if include_ics:
        attachments.append(
            {
                "type": "event_ics",
                "event_id": str(event.id),
                "filename": "event.ics",
                "content_type": "text/calendar",
            }
        )

    # Email to ticket holder
    ticket_holder_task = send_notification_email.s(
        recipient_email=user.email,
        subject=f"Ticket Update: {event.name}",
        template_txt="events/emails/ticket_update.txt",
        template_html="events/emails/ticket_update.html",
        context_ids={
            "user_id": str(user.id),
            "ticket_id": str(ticket.id),
            "event_id": str(event.id),
            "action": action,
        },
        attachments=attachments if attachments else None,
        user_id=str(user.id),
        notification_type=NotificationType.TICKET_UPDATED.value,
        event_id=str(event.id),
    )
    email_tasks.append(ticket_holder_task)

    # Notify organization staff and owners about new ticket
    if action in ["free_ticket_created", "offline_payment_pending", "at_door_payment_pending", "ticket_activated"]:
        staff_and_owners = get_organization_staff_and_owners(event.organization)

        for staff_user in staff_and_owners:
            staff_task = send_notification_email.s(
                recipient_email=staff_user.email,
                subject=f"New Ticket Issued: {event.name}",
                template_txt="events/emails/ticket_staff_notification.txt",
                template_html="events/emails/ticket_staff_notification.html",
                context_ids={
                    "user_id": str(staff_user.id),
                    "ticket_id": str(ticket.id),
                    "event_id": str(event.id),
                    "ticket_holder_id": str(user.id),
                    "action": action,
                },
                attachments=None,
                user_id=str(staff_user.id),
                notification_type=NotificationType.TICKET_CREATED.value,
                event_id=str(event.id),
            )
            email_tasks.append(staff_task)

    # Dispatch all emails in parallel
    if email_tasks:
        job = group(email_tasks)
        job.apply_async()

    logger.info(
        "ticket_update_notifications_dispatched",
        ticket_id=ticket_id,
        event_name=event.name,
        action=action,
        dispatched=len(email_tasks),
    )

    return {"dispatched": len(email_tasks)}


@shared_task
def notify_questionnaire_submission(questionnaire_submission_id: str) -> dict[str, int]:
    """Send notifications when a questionnaire is submitted for manual review.

    Dispatches emails in parallel. Each email is sent independently with
    automatic retry logic. Check logs and notification tracking for individual failures.

    Args:
        questionnaire_submission_id: The ID of the questionnaire submission

    Returns:
        Dictionary with dispatch and skip counts: {'dispatched': N, 'skipped': M}

    Raises:
        QuestionnaireSubmission.DoesNotExist: If submission not found
    """
    # Import here to avoid circular imports
    from questionnaires.models import QuestionnaireSubmission

    submission = QuestionnaireSubmission.objects.select_related("questionnaire__event__organization", "user").get(
        pk=questionnaire_submission_id
    )

    # Only notify if questionnaire is not in automatic mode
    if submission.questionnaire.evaluation_mode == submission.questionnaire.EvaluationMode.AUTOMATIC:
        logger.info(
            "questionnaire_submission_auto_skipped",
            questionnaire_id=str(submission.questionnaire.id),
            submission_id=questionnaire_submission_id,
        )
        return {"dispatched": 0, "skipped": 1}

    org_questionnaire = OrganizationQuestionnaire.objects.get(questionnaire_id=submission.questionnaire_id)
    organization = org_questionnaire.organization

    # Get staff and owners with evaluate_questionnaire permission
    staff_and_owners = get_organization_staff_and_owners(organization)
    # TODO: Filter by evaluate_questionnaire permission when permission system is implemented

    email_tasks = []
    skipped = 0

    for user in staff_and_owners:
        if not should_notify_user_for_questionnaire(user, organization, NotificationType.QUESTIONNAIRE_SUBMITTED):
            skipped += 1
            continue

        task = send_notification_email.s(
            recipient_email=user.email,
            subject=f"New Questionnaire Submission: {submission.questionnaire.name}",
            template_txt="events/emails/questionnaire_submission.txt",
            template_html="events/emails/questionnaire_submission.html",
            context_ids={
                "user_id": str(user.id),
                "submission_id": str(submission.id),
                "organization_id": str(organization.id),
            },
            attachments=None,
            user_id=str(user.id),
            notification_type=NotificationType.QUESTIONNAIRE_SUBMITTED.value,
            event_id=None,
        )
        email_tasks.append(task)

    # Dispatch all emails in parallel
    if email_tasks:
        job = group(email_tasks)
        job.apply_async()

    logger.info(
        "questionnaire_submission_notifications_dispatched",
        submission_id=questionnaire_submission_id,
        questionnaire_name=submission.questionnaire.name,
        dispatched=len(email_tasks),
        skipped=skipped,
    )

    return {"dispatched": len(email_tasks), "skipped": skipped}


@shared_task
def notify_questionnaire_evaluation_result(questionnaire_evaluation_id: str) -> dict[str, int]:
    """Send notification to user when their questionnaire evaluation is complete.

    Dispatches email asynchronously with automatic retry on failure.

    Args:
        questionnaire_evaluation_id: The ID of the questionnaire evaluation

    Returns:
        Dictionary with dispatch or skip count: {'dispatched': N, 'skipped': M}

    Raises:
        QuestionnaireEvaluation.DoesNotExist: If evaluation not found
    """
    # Import here to avoid circular imports
    from questionnaires.models import QuestionnaireEvaluation

    evaluation = QuestionnaireEvaluation.objects.select_related(
        "submission__questionnaire__event__organization", "submission__user", "evaluator"
    ).get(pk=questionnaire_evaluation_id)

    submission = evaluation.submission
    user = submission.user
    questionnaire = submission.questionnaire
    org_questionnaire = questionnaire.org_questionnaires
    organization = org_questionnaire.organization

    # Check if user should be notified
    if not should_notify_user_for_questionnaire(user, organization, NotificationType.QUESTIONNAIRE_EVALUATION):
        logger.info(
            "questionnaire_evaluation_notification_skipped",
            user_id=str(user.id),
            evaluation_id=questionnaire_evaluation_id,
            reason="user_preferences",
        )
        return {"dispatched": 0, "skipped": 1}

    # Dispatch email task
    send_notification_email.delay(
        recipient_email=user.email,
        subject=f"Questionnaire Evaluation Result: {questionnaire.name}",
        template_txt="events/emails/questionnaire_evaluation.txt",
        template_html="events/emails/questionnaire_evaluation.html",
        context_ids={
            "user_id": str(user.id),
            "evaluation_id": str(evaluation.id),
            "organization_id": str(organization.id),
        },
        attachments=None,
        user_id=str(user.id),
        notification_type=NotificationType.QUESTIONNAIRE_EVALUATION.value,  # Pass enum value as string
        event_id=None,
    )

    logger.info(
        "questionnaire_evaluation_notification_dispatched",
        evaluation_id=questionnaire_evaluation_id,
        questionnaire_name=questionnaire.name,
        user_email=user.email,
    )

    return {"dispatched": 1, "skipped": 0}


@shared_task(name="events.reset_demo_data")
def reset_demo_data() -> dict[str, str]:
    """Reset demo data by deleting organizations and example.com users, then re-bootstrapping.

    This task invokes the reset_events management command with --no-input flag.
    Only runs when DEMO_MODE is enabled.

    Returns:
        Dictionary with status information.
    """
    logger.info("Starting demo data reset task...")
    call_command("reset_events", "--no-input")
    logger.info("Demo data reset completed successfully")
    return {"status": "success", "message": "Demo data has been reset"}


# ---- Guest User Email Tasks ----


@shared_task
def send_guest_rsvp_confirmation(email: str, token: str, event_name: str) -> None:
    """Send RSVP confirmation email to guest user.

    Args:
        email: Guest user's email
        token: JWT confirmation token
        event_name: Name of the event
    """
    logger.info("guest_rsvp_confirmation_sending", email=email, event_name=event_name)
    subject = _("Confirm your RSVP to %(event_name)s") % {"event_name": event_name}
    confirmation_link = SiteSettings.get_solo().frontend_base_url + f"/events/confirm-action?token={token}"
    body = render_to_string(
        "events/emails/guest_rsvp_confirmation_body.txt",
        {"confirmation_link": confirmation_link, "event_name": event_name},
    )
    send_email(to=email, subject=subject, body=body)
    logger.info("guest_rsvp_confirmation_sent", email=email)


@shared_task
def send_guest_ticket_confirmation(email: str, token: str, event_name: str, tier_name: str) -> None:
    """Send ticket purchase confirmation email to guest user.

    Only sent for non-online-payment tickets (free/offline/at-the-door).

    Args:
        email: Guest user's email
        token: JWT confirmation token
        event_name: Name of the event
        tier_name: Name of the ticket tier
    """
    logger.info("guest_ticket_confirmation_sending", email=email, event_name=event_name, tier_name=tier_name)
    subject = _("Confirm your ticket for %(event_name)s") % {"event_name": event_name}
    confirmation_link = SiteSettings.get_solo().frontend_base_url + f"/events/confirm-action?token={token}"
    body = render_to_string(
        "events/emails/guest_ticket_confirmation_body.txt",
        {"confirmation_link": confirmation_link, "event_name": event_name, "tier_name": tier_name},
    )
    send_email(to=email, subject=subject, body=body)
    logger.info("guest_ticket_confirmation_sent", email=email)
