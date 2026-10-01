"""System announcements: platform-wide notices sent by Revel staff to users.

Shared by the admin "Send System Announcement" view and the
``send_system_announcement`` management command, so both build the same context,
target the same recipients and dispatch through the same Celery task.
"""

import functools
import typing as t
from collections.abc import Iterable

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Q, QuerySet
from django.utils import translation

from accounts.models import RevelUser
from common.fields import sanitize_html
from notifications.context_schemas import SystemAnnouncementContext
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.dispatcher import NotificationData, bulk_create_notifications
from notifications.tasks import dispatch_notifications_batch

logger = structlog.get_logger(__name__)

BATCH_SIZE = 500


class EmailPreview(t.NamedTuple):
    """Rendered email for one recipient, as the email channel would send it."""

    subject: str
    text_body: str


def build_context(title: str, body: str, url: str = "") -> SystemAnnouncementContext:
    """Build the notification context, sanitizing the HTML body.

    Args:
        title: Announcement title (in-app title and email subject).
        body: Announcement body as HTML (the Trix editor's output); sanitized with
            ``sanitize_html``, so only its allowlisted tags survive.
        url: Optional "Read more" link.

    Returns:
        The context stored on every notification.
    """
    context: SystemAnnouncementContext = {
        "announcement_title": title,
        "announcement_body": sanitize_html(body),
    }
    if url:
        context["policy_url"] = url
    return context


def get_recipients(
    *,
    include_guests: bool = False,
    exclude_user: RevelUser | None = None,
    emails: Iterable[str] | None = None,
) -> QuerySet[RevelUser]:
    """Return the active users an announcement goes to, ordered by primary key.

    Args:
        include_guests: Also include guest users (ignored when ``emails`` is given).
        exclude_user: A user to leave out (the admin sender).
        emails: Restrict to these addresses (case-insensitive), for previews. When
            given, the guest filter does not apply: the caller named the users.

    Returns:
        A queryset of recipients. Ordering keeps batch slicing stable.
    """
    users = RevelUser.objects.filter(is_active=True)
    if emails is not None:
        match_any = Q(pk__in=[])  # matches nothing, so an empty list sends to no one
        for email in emails:
            match_any |= Q(email__iexact=email.strip())
        users = users.filter(match_any)
    elif not include_guests:
        users = users.filter(guest=False)
    if exclude_user is not None:
        users = users.exclude(pk=exclude_user.pk)
    return users.order_by("pk")


def send(context: SystemAnnouncementContext, recipients: QuerySet[RevelUser]) -> int:
    """Create one notification per recipient and dispatch them in batches.

    Dispatch is deferred with ``transaction.on_commit``: inside a request
    (``ATOMIC_REQUESTS``) it fires when the request commits; from a management
    command it fires when the ``atomic`` block below commits. Either way the
    worker never sees an id that is not yet committed.

    Args:
        context: Context from :func:`build_context`.
        recipients: Recipients from :func:`get_recipients`.

    Returns:
        The number of notifications created.
    """
    total_created = 0
    with transaction.atomic():
        user_count = recipients.count()
        for batch_start in range(0, user_count, BATCH_SIZE):
            batch_users = recipients[batch_start : batch_start + BATCH_SIZE]
            created = bulk_create_notifications(
                [
                    NotificationData(
                        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
                        user=user,
                        context=dict(context),
                    )
                    for user in batch_users
                ]
            )
            batch_ids = [str(n.id) for n in created]
            # functools.partial binds batch_ids eagerly so each deferred dispatch
            # gets its own batch (a lambda would capture the loop variable and fire
            # the last batch repeatedly once the transaction commits).
            transaction.on_commit(functools.partial(dispatch_notifications_batch.delay, batch_ids))
            total_created += len(batch_ids)

    logger.info("system_announcement_sent", recipients=total_created)
    return total_created


def render_email_preview(context: SystemAnnouncementContext, user: RevelUser) -> EmailPreview:
    """Render the email ``user`` would receive, without creating anything.

    Args:
        context: Context from :func:`build_context`.
        user: The recipient to render for (language, unsubscribe link).

    Returns:
        The subject and plain-text body.
    """
    from notifications.service.templates.registry import get_template

    notification = Notification(
        notification_type=NotificationType.SYSTEM_ANNOUNCEMENT,
        user=user,
        context=dict(context),
    )
    template = get_template(NotificationType.SYSTEM_ANNOUNCEMENT)
    with translation.override(getattr(user, "language", settings.LANGUAGE_CODE)):
        return EmailPreview(
            subject=template.get_email_subject(notification),
            text_body=template.get_email_text_body(notification),
        )
