# src/telegram/signals.py

import logging
import typing as t

from django.db.models.signals import post_save
from django.dispatch import receiver

from events.models import EventInvitation
from telegram.tasks import send_event_invitation_task

logger = logging.getLogger(__name__)


@receiver(post_save, sender=EventInvitation)
def handle_invitation_creation(
    sender: type[EventInvitation], instance: EventInvitation, created: bool, **kwargs: t.Any
) -> None:
    """Triggers a Celery task to send a Telegram invitation when a new EventInvitation is created."""
    if created:
        logger.info(f"New EventInvitation created (ID: {instance.id}). Queuing Telegram notification.")
        send_event_invitation_task.delay(str(instance.id))
