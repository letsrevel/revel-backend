import logging
from collections import Counter
from uuid import UUID

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import RevelUser
from common.models import EmailLog, SiteSettings
from common.tasks import to_safe_email_address
from events.service import update_db_instance
from events.utils import create_ticket_pdf

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

logger = logging.getLogger(__name__)


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
def send_payment_confirmation_email(payment_id: str) -> None:
    """Sends a payment confirmation email to the user with PDF and ICS attachments."""
    payment = Payment.objects.select_related(
        "user", "ticket__event__organization", "ticket__event__city", "ticket__tier"
    ).get(pk=payment_id)
    user = payment.user
    ticket = payment.ticket

    subject = f"Your Ticket for {ticket.event.name}"
    context = {"user": user, "ticket": ticket, "payment": payment}
    body = render_to_string("events/emails/payment_confirmation.txt", context)
    html_body = render_to_string("events/emails/payment_confirmation.html", context)

    # Generate PDF and ICS files
    pdf_bytes = create_ticket_pdf(ticket)
    ics_bytes = ticket.event.ics()

    # Build and send email directly to support attachments
    site_settings = SiteSettings.get_solo()
    recipient = to_safe_email_address(user.email, site_settings=site_settings)

    email_msg = EmailMultiAlternatives(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        bcc=[recipient],
    )
    if html_body:
        email_msg.attach_alternative(html_body, "text/html")

    # Attach generated files
    email_msg.attach("ticket.pdf", pdf_bytes, "application/pdf")
    email_msg.attach("invite.ics", ics_bytes, "text/calendar")

    email_msg.send(fail_silently=False)

    # Log the email for auditing purposes
    email_log = EmailLog(to=recipient, subject=subject)
    email_log.set_body(body=body)
    if html_body:
        email_log.set_html(html_body=html_body)
    email_log.save()


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

    Args:
        event_id: The ID of the event that was opened

    Returns:
        Dictionary with notification statistics
    """
    try:
        event = Event.objects.select_related("organization").get(pk=event_id)
    except Event.DoesNotExist:
        logger.error(f"Event with ID {event_id} not found for notification")
        return {"error": 1, "sent": 0, "skipped": 0}

    eligible_users = get_eligible_users_for_event_notification(event, NotificationType.EVENT_OPEN)

    stats = {"sent": 0, "skipped": 0, "error": 0}

    for user in eligible_users:
        try:
            subject = f"New Event: {event.name}"
            context = {
                "user": user,
                "event": event,
                "organization": event.organization,
            }

            body = render_to_string("events/emails/event_open.txt", context)
            html_body = render_to_string("events/emails/event_open.html", context)

            # Generate ICS attachment
            ics_bytes = event.ics()

            # Build email with attachment
            site_settings = SiteSettings.get_solo()
            recipient = to_safe_email_address(user.email, site_settings=site_settings)

            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                bcc=[recipient],
            )

            if html_body:
                email_msg.attach_alternative(html_body, "text/html")

            # Attach ICS file
            email_msg.attach("event.ics", ics_bytes, "text/calendar")

            email_msg.send(fail_silently=False)

            # Log the email
            email_log = EmailLog(to=recipient, subject=subject)
            email_log.set_body(body=body)
            if html_body:
                email_log.set_html(html_body=html_body)
            email_log.save()

            log_notification_attempt(user, NotificationType.EVENT_OPEN, event=event, success=True)
            stats["sent"] += 1

        except Exception as e:
            logger.exception(f"Failed to send event open notification to {user.email}")
            log_notification_attempt(
                user, NotificationType.EVENT_OPEN, event=event, success=False, error_message=str(e)
            )
            stats["error"] += 1

    logger.info(f"Event open notifications for {event.name}: {stats['sent']} sent, {stats['error']} errors")

    return stats


@shared_task
def notify_potluck_item_update(
    potluck_item_id: str, action: str, changed_by_user_id: str | None = None
) -> dict[str, int]:
    """Send notifications for potluck item updates.

    Args:
        potluck_item_id: The ID of the potluck item that was updated
        action: The action performed ('created', 'deleted', 'assigned', 'unassigned')
        changed_by_user_id: ID of user who made the change (optional)

    Returns:
        Dictionary with notification statistics
    """
    try:
        potluck_item = PotluckItem.objects.select_related("event__organization", "assignee").get(pk=potluck_item_id)
        event = potluck_item.event
    except PotluckItem.DoesNotExist:
        logger.error(f"PotluckItem with ID {potluck_item_id} not found for notification")
        return {"error": 1, "sent": 0, "skipped": 0}

    # Get eligible users (including staff and owners)
    eligible_users = get_eligible_users_for_event_notification(event, NotificationType.POTLUCK_UPDATE)
    staff_and_owners = get_organization_staff_and_owners(event.organization)

    # Combine and deduplicate using IDs to avoid queryset combination issues
    eligible_user_ids = set(eligible_users.values_list("id", flat=True))
    staff_and_owner_ids = set(staff_and_owners.values_list("id", flat=True))
    all_user_ids = eligible_user_ids | staff_and_owner_ids
    all_users = RevelUser.objects.filter(id__in=all_user_ids)

    # Get the user who made the change (to avoid self-notification)
    changed_by = None
    if changed_by_user_id:
        try:
            changed_by = RevelUser.objects.get(pk=changed_by_user_id)
        except RevelUser.DoesNotExist:
            pass

    stats = {"sent": 0, "skipped": 0, "error": 0}

    for user in all_users:
        # Skip self-notification
        if changed_by and user.id == changed_by.id:
            stats["skipped"] += 1
            continue

        try:
            subject = f"Potluck Update: {event.name}"
            context = {
                "user": user,
                "event": event,
                "potluck_item": potluck_item,
                "action": action,
                "changed_by": changed_by,
            }

            body = render_to_string("events/emails/potluck_update.txt", context)
            html_body = render_to_string("events/emails/potluck_update.html", context)

            site_settings = SiteSettings.get_solo()
            recipient = to_safe_email_address(user.email, site_settings=site_settings)

            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                bcc=[recipient],
            )

            if html_body:
                email_msg.attach_alternative(html_body, "text/html")

            email_msg.send(fail_silently=False)

            # Log the email
            email_log = EmailLog(to=recipient, subject=subject)
            email_log.set_body(body=body)
            if html_body:
                email_log.set_html(html_body=html_body)
            email_log.save()

            log_notification_attempt(user, NotificationType.POTLUCK_UPDATE, event=event, success=True)
            stats["sent"] += 1

        except Exception as e:
            logger.exception(f"Failed to send potluck update notification to {user.email}")
            log_notification_attempt(
                user, NotificationType.POTLUCK_UPDATE, event=event, success=False, error_message=str(e)
            )
            stats["error"] += 1

    logger.info(
        f"Potluck {action} notifications for {event.name}: "
        f"{stats['sent']} sent, {stats['error']} errors, {stats['skipped']} skipped"
    )

    return stats


@shared_task
def notify_ticket_update(  # noqa: C901  # todo: refactor
    ticket_id: str, action: str, include_pdf: bool = True, include_ics: bool = True
) -> dict[str, int]:
    """Send notifications for ticket updates.

    Args:
        ticket_id: The ID of the ticket that was updated
        action: The action performed (e.g., 'created', 'activated', 'payment_pending')
        include_pdf: Whether to include ticket PDF attachment
        include_ics: Whether to include event ICS attachment

    Returns:
        Dictionary with notification statistics
    """
    try:
        ticket = Ticket.objects.select_related("user", "event__organization", "tier").get(pk=ticket_id)
    except Ticket.DoesNotExist:
        logger.error(f"Ticket with ID {ticket_id} not found for notification")
        return {"error": 1, "sent": 0, "skipped": 0}

    user = ticket.user
    event = ticket.event

    stats = {"sent": 0, "skipped": 0, "error": 0}

    try:
        # Notify the ticket holder
        subject = f"Ticket Update: {event.name}"
        context = {
            "user": user,
            "ticket": ticket,
            "event": event,
            "action": action,
        }

        body = render_to_string("events/emails/ticket_update.txt", context)
        html_body = render_to_string("events/emails/ticket_update.html", context)

        site_settings = SiteSettings.get_solo()
        recipient = to_safe_email_address(user.email, site_settings=site_settings)

        email_msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            bcc=[recipient],
        )

        if html_body:
            email_msg.attach_alternative(html_body, "text/html")

        # Add attachments if requested
        if include_pdf and ticket.status == Ticket.Status.ACTIVE:
            try:
                pdf_bytes = create_ticket_pdf(ticket)
                email_msg.attach("ticket.pdf", pdf_bytes, "application/pdf")
            except Exception as e:
                logger.warning(f"Failed to generate PDF for ticket {ticket_id}: {e}")

        if include_ics:
            try:
                ics_bytes = event.ics()
                email_msg.attach("event.ics", ics_bytes, "text/calendar")
            except Exception as e:
                logger.warning(f"Failed to generate ICS for event {event.id}: {e}")

        email_msg.send(fail_silently=False)

        # Log the email
        email_log = EmailLog(to=recipient, subject=subject)
        email_log.set_body(body=body)
        if html_body:
            email_log.set_html(html_body=html_body)
        email_log.save()

        log_notification_attempt(user, NotificationType.TICKET_UPDATED, event=event, success=True)
        stats["sent"] += 1

    except Exception as e:
        logger.exception(f"Failed to send ticket update notification to {user.email}")
        log_notification_attempt(
            user, NotificationType.TICKET_UPDATED, event=event, success=False, error_message=str(e)
        )
        stats["error"] += 1

    # Notify organization staff and owners about new ticket
    if action in ["free_ticket_created", "offline_payment_pending", "at_door_payment_pending", "ticket_activated"]:
        staff_and_owners = get_organization_staff_and_owners(event.organization)

        for staff_user in staff_and_owners:
            try:
                subject = f"New Ticket Issued: {event.name}"
                context = {
                    "user": staff_user,
                    "ticket": ticket,
                    "event": event,
                    "ticket_holder": user,
                    "action": action,
                }

                body = render_to_string("events/emails/ticket_staff_notification.txt", context)
                html_body = render_to_string("events/emails/ticket_staff_notification.html", context)

                recipient = to_safe_email_address(staff_user.email, site_settings=site_settings)

                email_msg = EmailMultiAlternatives(
                    subject=subject,
                    body=body,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    bcc=[recipient],
                )

                if html_body:
                    email_msg.attach_alternative(html_body, "text/html")

                email_msg.send(fail_silently=False)

                # Log the email
                email_log = EmailLog(to=recipient, subject=subject)
                email_log.set_body(body=body)
                if html_body:
                    email_log.set_html(html_body=html_body)
                email_log.save()

                log_notification_attempt(staff_user, NotificationType.TICKET_CREATED, event=event, success=True)
                stats["sent"] += 1

            except Exception as e:
                logger.exception(f"Failed to send staff ticket notification to {staff_user.email}")
                log_notification_attempt(
                    staff_user, NotificationType.TICKET_CREATED, event=event, success=False, error_message=str(e)
                )
                stats["error"] += 1

    logger.info(
        f"Ticket {action} notifications for {event.name}: "
        f"{stats['sent']} sent, {stats['error']} errors, {stats['skipped']} skipped"
    )

    return stats


@shared_task
def notify_questionnaire_submission(questionnaire_submission_id: str) -> dict[str, int]:
    """Send notifications when a questionnaire is submitted for manual review.

    Args:
        questionnaire_submission_id: The ID of the questionnaire submission

    Returns:
        Dictionary with notification statistics
    """
    # Import here to avoid circular imports
    from questionnaires.models import QuestionnaireSubmission

    try:
        submission = QuestionnaireSubmission.objects.select_related("questionnaire__event__organization", "user").get(
            pk=questionnaire_submission_id
        )
    except QuestionnaireSubmission.DoesNotExist:
        logger.error(f"QuestionnaireSubmission with ID {questionnaire_submission_id} not found")
        return {"error": 1, "sent": 0, "skipped": 0}

    # Only notify if questionnaire is not in automatic mode
    if submission.questionnaire.evaluation_mode == submission.questionnaire.EvaluationMode.AUTOMATIC:
        logger.info(f"Skipping notification for automatic questionnaire {submission.questionnaire.id}")
        return {"sent": 0, "skipped": 1, "error": 0}

    org_questionnaire = OrganizationQuestionnaire.objects.get(questionnaire_id=submission.questionnaire_id)
    organization = org_questionnaire.organization

    # Get staff and owners with evaluate_questionnaire permission
    staff_and_owners = get_organization_staff_and_owners(organization)
    # TODO: Filter by evaluate_questionnaire permission when permission system is implemented

    stats = {"sent": 0, "skipped": 0, "error": 0}

    for user in staff_and_owners:
        if not should_notify_user_for_questionnaire(user, organization, NotificationType.QUESTIONNAIRE_SUBMITTED):
            stats["skipped"] += 1
            continue

        try:
            subject = f"New Questionnaire Submission: {submission.questionnaire.name}"
            context = {
                "user": user,
                "submission": submission,
                "questionnaire": submission.questionnaire,
                "organization": organization,
                "submitter": submission.user,
            }

            body = render_to_string("events/emails/questionnaire_submission.txt", context)
            html_body = render_to_string("events/emails/questionnaire_submission.html", context)

            site_settings = SiteSettings.get_solo()
            recipient = to_safe_email_address(user.email, site_settings=site_settings)

            email_msg = EmailMultiAlternatives(
                subject=subject,
                body=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                bcc=[recipient],
            )

            if html_body:
                email_msg.attach_alternative(html_body, "text/html")

            email_msg.send(fail_silently=False)

            # Log the email
            email_log = EmailLog(to=recipient, subject=subject)
            email_log.set_body(body=body)
            if html_body:
                email_log.set_html(html_body=html_body)
            email_log.save()

            log_notification_attempt(user, NotificationType.QUESTIONNAIRE_SUBMITTED, event=None, success=True)
            stats["sent"] += 1

        except Exception as e:
            logger.exception(f"Failed to send questionnaire submission notification to {user.email}")
            log_notification_attempt(
                user, NotificationType.QUESTIONNAIRE_SUBMITTED, event=None, success=False, error_message=str(e)
            )
            stats["error"] += 1

    return stats


@shared_task
def notify_questionnaire_evaluation_result(questionnaire_evaluation_id: str) -> dict[str, int]:
    """Send notification to user when their questionnaire evaluation is complete.

    Args:
        questionnaire_evaluation_id: The ID of the questionnaire evaluation

    Returns:
        Dictionary with notification statistics
    """
    # Import here to avoid circular imports
    from questionnaires.models import QuestionnaireEvaluation

    try:
        evaluation = QuestionnaireEvaluation.objects.select_related(
            "submission__questionnaire__event__organization", "submission__user", "evaluator"
        ).get(pk=questionnaire_evaluation_id)
    except QuestionnaireEvaluation.DoesNotExist:
        logger.error(f"QuestionnaireEvaluation with ID {questionnaire_evaluation_id} not found")
        return {"error": 1, "sent": 0, "skipped": 0}

    submission = evaluation.submission
    user = submission.user
    questionnaire = submission.questionnaire
    org_questionnaire = questionnaire.org_questionnaires
    organization = org_questionnaire.organization

    stats = {"sent": 0, "skipped": 0, "error": 0}

    # Check if user should be notified
    if not should_notify_user_for_questionnaire(user, organization, NotificationType.QUESTIONNAIRE_EVALUATION):
        stats["skipped"] = 1
        return stats

    try:
        subject = f"Questionnaire Evaluation Result: {questionnaire.name}"
        context = {
            "user": user,
            "evaluation": evaluation,
            "submission": submission,
            "questionnaire": submission.questionnaire,
            "organization": organization,
        }

        body = render_to_string("events/emails/questionnaire_evaluation.txt", context)
        html_body = render_to_string("events/emails/questionnaire_evaluation.html", context)

        site_settings = SiteSettings.get_solo()
        recipient = to_safe_email_address(user.email, site_settings=site_settings)

        email_msg = EmailMultiAlternatives(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            bcc=[recipient],
        )

        if html_body:
            email_msg.attach_alternative(html_body, "text/html")

        email_msg.send(fail_silently=False)

        # Log the email
        email_log = EmailLog(to=recipient, subject=subject)
        email_log.set_body(body=body)
        if html_body:
            email_log.set_html(html_body=html_body)
        email_log.save()

        log_notification_attempt(user, NotificationType.QUESTIONNAIRE_EVALUATION, event=None, success=True)
        stats["sent"] = 1

    except Exception as e:
        logger.exception(f"Failed to send questionnaire evaluation result to {user.email}")
        log_notification_attempt(
            user, NotificationType.QUESTIONNAIRE_EVALUATION, event=None, success=False, error_message=str(e)
        )
        stats["error"] = 1

    logger.info(
        f"Questionnaire evaluation result notification for {questionnaire.name}: "
        f"{stats['sent']} sent, {stats['error']} errors, {stats['skipped']} skipped"
    )

    return stats
