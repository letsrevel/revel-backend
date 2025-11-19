# src/telegram/handlers/admin.py

import structlog
from aiogram import F, Router
from aiogram.filters import (
    CommandStart,
    StateFilter,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InaccessibleMessage, Message

from accounts.models import RevelUser
from telegram import keyboards
from telegram.fsm import BroadcastStates
from telegram.middleware import AuthorizationMiddleware
from telegram.models import TelegramUser
from telegram.tasks import send_broadcast_message_task

logger = structlog.get_logger(__name__)
router = Router()

# Register middleware at router level to access handler flags
router.message.middleware(AuthorizationMiddleware())
router.callback_query.middleware(AuthorizationMiddleware())


# --- Superuser Broadcast Feature ---


# This handler will attempt to catch any non-command text message from a superuser.
# IMPORTANT: Read the note on handler order and potential conflicts above.
@router.message(
    F.text,  # Matches any text message
    ~CommandStart(),  # Excludes messages starting with '/' (commands)
    flags={"requires_superuser": True},
)
async def handle_potential_broadcast_message(
    message: Message, user: RevelUser, tg_user: TelegramUser, state: FSMContext
) -> None:
    """If a raw message is received, and it is sent by a superuser, it can be broadcast to all users."""
    if not await _check_broadcast_gates(message, user, state):
        return

    await state.set_state(BroadcastStates.confirming_broadcast)
    await state.update_data(broadcast_message_text=message.text, broadcast_message_html_text=message.html_text)

    # Use message.html_text to preserve any formatting the superuser might have used.
    # The <blockquote> tag is good for quoting in HTML parse mode.
    confirmation_prompt = (
        "⚠️ **BROADCAST CONFIRMATION** ⚠️\n\n"
        "Are you sure you want to broadcast this exact message to ALL active Telegram users?"
    )
    await message.reply(  # Using reply to make it clear which message is being considered
        confirmation_prompt, reply_markup=keyboards.get_broadcast_confirmation_keyboard(), parse_mode="HTML"
    )


async def _check_broadcast_gates(message: Message, user: RevelUser, state: FSMContext) -> bool:
    """Checks whether a broadcast message can be sent."""
    current_fsm_state = await state.get_state()
    if current_fsm_state is not None:
        # Superuser is in an active FSM flow (e.g., setting preferences).
        # Do not interpret this message as a broadcast request.
        logger.debug(
            "superuser_broadcast_attempt_in_state",
            username=user.username,
            state=current_fsm_state,
        )
        return False

    # Superuser, not a command, and not in an active FSM state. Treat as potential broadcast.
    if not message.text or not message.text.strip():  # Ignore empty messages
        logger.debug("superuser_empty_broadcast_message", username=user.username)
        return False

    return True


@router.callback_query(
    StateFilter(BroadcastStates.confirming_broadcast),
    F.data.startswith("broadcast_confirm:"),
    flags={"requires_superuser": True},
)
async def cb_broadcast_confirm(
    callback: CallbackQuery, user: RevelUser, tg_user: TelegramUser, state: FSMContext
) -> None:
    """Confirm send broadcast message to all Telegram users."""
    assert callback.data is not None
    action = callback.data.split(":")[1]
    data = await state.get_data()
    message_text_to_broadcast = data.get("broadcast_message_html_text")  # Get the raw text

    await state.clear()  # Clear state regardless of action
    assert callback.message is not None
    original_confirmation_message = callback.message
    if not isinstance(original_confirmation_message, InaccessibleMessage):
        # Edit the original confirmation message to remove buttons and indicate action
        if action == "yes":
            await original_confirmation_message.edit_text(
                f"<blockquote>{data.get('broadcast_message_html_text')}</blockquote>\n\n✅ Broadcast initiated.",
                parse_mode="HTML",
            )
        else:
            await original_confirmation_message.edit_text(
                f"<blockquote>{data.get('broadcast_message_html_text')}</blockquote>\n\n"
                f"❌ Broadcast cancelled by user.",
                parse_mode="HTML",
            )

    if action == "yes":
        if message_text_to_broadcast:
            task_info = send_broadcast_message_task.delay(message_text_to_broadcast)
            await callback.answer(f"Broadcast task queued (ID: {task_info.id}).", show_alert=True)
            logger.info(
                "superuser_confirmed_broadcast",
                username=user.username,
                message_preview=message_text_to_broadcast[:100],
                task_id=task_info.id,
            )
        else:
            await callback.answer("Error: No message content found to broadcast.", show_alert=True)
            logger.error(
                "broadcast_message_missing_from_fsm",
                username=user.username,
            )
    else:  # action == "no"
        await callback.answer("Broadcast cancelled.", show_alert=False)  # No need for alert if message edited
        logger.info("superuser_cancelled_broadcast", username=user.username)
