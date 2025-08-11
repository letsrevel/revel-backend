# src/telegram/handlers/admin.py

import logging

from aiogram import F, Router
from aiogram.filters import (
    CommandStart,
    StateFilter,  # Added StateFilter
)
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InaccessibleMessage, Message  # Added CallbackQuery, InaccessibleMessage
from django.contrib.auth.models import User as DjangoUser

from telegram import keyboards
from telegram.fsm import BroadcastStates  # NEW: Import BroadcastStates
from telegram.tasks import send_broadcast_message_task  # NEW: Import the task

logger = logging.getLogger(__name__)
router = Router()


# --- Superuser Broadcast Feature ---


# This handler will attempt to catch any non-command text message from a superuser.
# IMPORTANT: Read the note on handler order and potential conflicts above.
@router.message(
    F.text,  # Matches any text message
    ~CommandStart(),  # Excludes messages starting with '/' (commands)
    # Superuser check is done inside the handler to allow other F.text handlers for non-superusers
    # to be processed if this handler is registered before them.
)
async def handle_potential_broadcast_message(message: Message, user: DjangoUser, state: FSMContext) -> None:
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


async def _check_broadcast_gates(message: Message, user: DjangoUser, state: FSMContext) -> bool:
    """Checks whether a broadcast message can be sent."""
    if not user.is_superuser:
        logger.debug(f"User {user} has no permission to broadcast message")
        return False

    current_fsm_state = await state.get_state()
    if current_fsm_state is not None:
        # Superuser is in an active FSM flow (e.g., setting preferences).
        # Do not interpret this message as a broadcast request.
        logger.debug(
            f"Superuser {user.username} attempting broadcast while in state {current_fsm_state}. "
            f"Not treating as broadcast."
        )
        return False

    # Superuser, not a command, and not in an active FSM state. Treat as potential broadcast.
    if not message.text or not message.text.strip():  # Ignore empty messages
        logger.debug(f"Superuser {user.username} sent an empty message. Not broadcasting.")
        return False

    return True


@router.callback_query(StateFilter(BroadcastStates.confirming_broadcast), F.data.startswith("broadcast_confirm:"))
async def cb_broadcast_confirm(callback: CallbackQuery, user: DjangoUser, state: FSMContext) -> None:
    """Confirm send broadcast message to all Telegram users."""
    if not user.is_superuser:  # Should be protected by state, but double-check
        await callback.answer("This action is for superusers only.", show_alert=True)
        await state.clear()
        if callback.message and not isinstance(callback.message, InaccessibleMessage):
            await callback.message.delete()  # Clean up confirmation message
        return

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
                f'Superuser {user.username} confirmed broadcast for: "{message_text_to_broadcast[:100]}...". '
                f"Task ID: {task_info.id}"
            )
        else:
            await callback.answer("Error: No message content found to broadcast.", show_alert=True)
            logger.error(
                f"Superuser {user.username} confirmed broadcast, "
                f"but 'broadcast_message_text' was missing from FSM data."
            )
    else:  # action == "no"
        await callback.answer("Broadcast cancelled.", show_alert=False)  # No need for alert if message edited
        logger.info(f"Superuser {user.username} cancelled broadcast.")
