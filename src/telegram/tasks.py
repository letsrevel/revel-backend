import importlib
import typing as t

import structlog
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from asgiref.sync import async_to_sync
from celery import Task, shared_task
from django.db import transaction

from events import utils as event_utils
from events.models import EventInvitation
from events.service.event_manager import EligibilityService
from notifications.enums import DeliveryStatus
from telegram import utils
from telegram.keyboards import get_event_eligible_keyboard
from telegram.models import TelegramUser

logger = structlog.getLogger(__name__)


# --- Async Helper Functions ---


def _execute_callback(callback_data: dict[str, t.Any], error_occurred: bool, error_message: str | None) -> None:
    """Execute callback function after telegram message delivery attempt.

    Args:
        callback_data: Callback configuration with module, function, and kwargs
        error_occurred: Whether an error occurred during message sending
        error_message: Error message if error occurred
    """
    try:
        module_path = callback_data.get("module")
        function_name = callback_data.get("function")
        kwargs = callback_data.get("kwargs", {})

        if not module_path or not function_name:
            logger.error("Invalid callback_data: missing module or function", callback_data=callback_data)
            return

        # Import module and get function
        module = importlib.import_module(module_path)
        callback_function = getattr(module, function_name)

        # Add status and error_message to kwargs
        if error_occurred:
            kwargs["status"] = DeliveryStatus.FAILED
            kwargs["error_message"] = error_message
        else:
            kwargs["status"] = DeliveryStatus.SENT

        # Execute callback
        callback_function(**kwargs)

        logger.info(
            "telegram_callback_executed",
            module=module_path,
            function=function_name,
            status=DeliveryStatus.FAILED if error_occurred else DeliveryStatus.SENT,
        )
    except Exception as callback_error:
        logger.error(
            "telegram_callback_failed",
            callback_data=callback_data,
            error=str(callback_error),
        )


@shared_task(bind=True, name="telegram.send_message_task", rate_limit="20/s", queue="telegram")
def send_message_task(
    self: Task,  # type: ignore[type-arg]
    telegram_id: int,
    *,
    message: str,
    callback_data: dict[str, t.Any] | None = None,
) -> None:
    """Wrapper for async message sending task with optional callback.

    Args:
        self: Celery task instance
        telegram_id: Telegram user ID
        message: Message to send
        callback_data: Optional callback configuration with:
            - module: Python module path (e.g., "notifications.service.channels.telegram")
            - function: Function name to call (e.g., "update_delivery_status")
            - kwargs: Dict of keyword arguments to pass to the function
    """
    logger.info(f"telegram.tasks.send_message_task({telegram_id=}, {message=}, callback={bool(callback_data)})")

    error_occurred = False
    error_message = None

    try:
        async_to_sync(utils.send_telegram_message)(telegram_id, message=message)
    except TelegramForbiddenError as e:
        error_occurred = True
        if "bot was blocked by the user" in e.message.lower():
            logger.warning(f"User {telegram_id} blocked the bot.")
            TelegramUser.objects.filter(telegram_id=telegram_id).update(blocked_by_user=True)
            error_message = "User blocked the bot"
        elif "user is deactivated" in e.message.lower():
            logger.warning(f"User {telegram_id} is deactivated.")
            TelegramUser.objects.filter(telegram_id=telegram_id).update(user_is_deactivated=True)
            error_message = "User is deactivated"
        else:
            error_message = str(e)
    except TelegramRetryAfter as e:
        logger.warning(f"Telegram API rate limit exceeded. Retrying in {e.retry_after} seconds.")
        raise self.retry(exc=e, countdown=e.retry_after)
    except Exception as e:
        error_occurred = True
        error_message = str(e)
        logger.error(f"Error sending message to Telegram ID {telegram_id}: {e}")
    else:
        logger.info(f"Successfully sent message to Telegram ID {telegram_id} ({message=})")

    # Execute callback if provided
    if callback_data:
        _execute_callback(callback_data, error_occurred, error_message)


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
