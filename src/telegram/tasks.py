import importlib
import typing as t

import structlog
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from asgiref.sync import async_to_sync
from celery import Task, shared_task

from notifications.enums import DeliveryStatus
from telegram import utils
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


@shared_task(bind=True, name="telegram.send_message_task", rate_limit="20/s")
def send_message_task(
    self: Task,  # type: ignore[type-arg]
    telegram_id: int,
    *,
    message: str,
    reply_markup: dict[str, t.Any] | None = None,
    callback_data: dict[str, t.Any] | None = None,
    qr_data: str | None = None,
) -> None:
    """Wrapper for async message sending task with optional callback and QR code photo attachment.

    Args:
        self: Celery task instance
        telegram_id: Telegram user ID
        message: Message to send
        reply_markup: Optional serialized InlineKeyboardMarkup dict
        callback_data: Optional callback configuration with:
            - module: Python module path (e.g., "notifications.service.channels.telegram")
            - function: Function name to call (e.g., "update_delivery_status")
            - kwargs: Dict of keyword arguments to pass to the function
        qr_data: Optional data to generate QR code photo from (e.g., ticket ID)
    """
    logger.info(
        f"telegram.tasks.send_message_task({telegram_id=}, {message=}, "
        f"reply_markup={bool(reply_markup)}, callback={bool(callback_data)}, qr={bool(qr_data)})"
    )

    error_occurred = False
    error_message = None
    unexpected_exception: Exception | None = None

    try:
        # Deserialize reply_markup if provided
        keyboard = InlineKeyboardMarkup.model_validate(reply_markup) if reply_markup else None

        # Generate QR code photo if qr_data provided
        photo = utils.generate_qr_code(qr_data) if qr_data else None

        async_to_sync(utils.send_telegram_message)(telegram_id, message=message, reply_markup=keyboard, photo=photo)
    except TelegramForbiddenError as e:
        # Expected business states - user blocked bot or account deactivated
        # Not task failures, just states we handle gracefully
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
        # Unexpected error - store for re-raising after callback
        error_occurred = True
        error_message = str(e)
        unexpected_exception = e
        logger.error(f"Error sending message to Telegram ID {telegram_id}: {e}")
    else:
        logger.info(f"Successfully sent message to Telegram ID {telegram_id} ({message=})")

    # Execute callback if provided (before re-raising, so delivery status is tracked)
    if callback_data:
        _execute_callback(callback_data, error_occurred, error_message)

    # Re-raise unexpected exceptions so Celery marks task as failed
    if unexpected_exception is not None:
        raise unexpected_exception


@shared_task(name="telegram.send_broadcast_message_task")
def send_broadcast_message_task(message: str) -> int:
    """Sends a broadcast message to all Telegram users."""
    logger.info(f"telegram.tasks.send_broadcast_message_task({message=})")
    telegram_users = TelegramUser.objects.active_users()
    for telegram_user in telegram_users.iterator(chunk_size=1000):
        send_message_task.delay(telegram_user.telegram_id, message=message)
    total = telegram_users.count()
    logger.info(f"Queued {total} Telegram users to broadcast.")
    return total
