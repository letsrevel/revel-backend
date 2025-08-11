import logging

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from asgiref.sync import async_to_sync
from celery import Task, shared_task
from django.db import transaction

from events import utils as event_utils
from events.models import EventInvitation
from events.service.event_manager import EligibilityService
from telegram import utils
from telegram.keyboards import get_event_eligible_keyboard
from telegram.models import TelegramUser

logger = logging.getLogger(__name__)


# --- Async Helper Functions ---


@shared_task(bind=True, name="telegram.send_message_task", rate_limit="20/s", queue="telegram")
def send_message_task(
    self: Task,  # type: ignore[type-arg]
    telegram_id: int,
    *,
    message: str,
) -> None:
    """Wrapper for async message sending task."""
    logger.info(f"telegram.tasks.send_message_task({telegram_id=}, {message=})")
    try:
        async_to_sync(utils.send_telegram_message)(telegram_id, message=message)
    except TelegramForbiddenError as e:
        if "bot was blocked by the user" in e.message.lower():
            logger.warning(f"User {telegram_id} blocked the bot.")
            TelegramUser.objects.filter(telegram_id=telegram_id).update(blocked_by_user=True)
            return
        if "user is deactivated" in e.message.lower():
            logger.warning(f"User {telegram_id} is deactivated.")
            TelegramUser.objects.filter(telegram_id=telegram_id).update(user_is_deactivated=True)
            return
    except TelegramRetryAfter as e:
        logger.warning(f"Telegram API rate limit exceeded. Retrying in {e.retry_after} seconds.")
        raise self.retry(exc=e, countdown=e.retry_after)
    except Exception as e:
        logger.error(f"Error sending message to Telegram ID {telegram_id}: {e}")
    else:
        logger.info(f"Successfully sent message to Telegram ID {telegram_id} ({message=})")


@shared_task(name="telegram.send_broadcast_message_task", queue="telegram")
def send_broadcast_message_task(message: str) -> int:
    """Sends a broadcast message to all Telegram users."""
    logger.info(f"telegram.tasks.send_broadcast_message_task({message=})")
    telegram_users = TelegramUser.objects.active_users()
    for telegram_user in telegram_users.iterator(chunk_size=1000):
        send_message_task.delay(telegram_user.telegram_id, message=message)
    total = telegram_users.count()
    logger.info(f"Queued {total} Telegram users to broadcast.")
    return total


@shared_task(name="telegram.send_event_invitation_task", queue="telegram")
@transaction.atomic
def send_event_invitation_task(invitation_id: str) -> None:
    """Fetches an event invitation and sends a message to the user via Telegram.

    with the appropriate keyboard based on their eligibility.
    """
    try:
        invitation = EventInvitation.objects.select_related("user", "event").get(id=invitation_id)
    except EventInvitation.DoesNotExist:
        logger.error(f"EventInvitation with ID {invitation_id} not found.")
        return

    user = invitation.user
    event = invitation.event
    tg_user = TelegramUser.objects.filter(user=user).first()
    if not tg_user:
        logger.warning(
            f"No TelegramUser found for Django user {user.username} (ID: {user.id}). Cannot send invitation."
        )
        return

    # Use the TicketHandler to determine the next steps
    handler = EligibilityService(user=user, event=event)
    eligibility = handler.check_eligibility()

    # Construct the message and keyboard based on eligibility
    message_text = event_utils.get_invitation_message(user, event)

    reply_markup = get_event_eligible_keyboard(event, eligibility, user)

    async_to_sync(utils.send_telegram_message)(tg_user.telegram_id, message=message_text, reply_markup=reply_markup)
