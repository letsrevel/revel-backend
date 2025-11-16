# src/telegram/handlers/common.py

import logging
import re
import typing as t

from aiogram import F, Router
from aiogram.filters import (
    Command,
    CommandStart,
)
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from asgiref.sync import sync_to_async

from accounts.models import RevelUser
from common.models import Legal
from telegram import keyboards
from telegram.models import AccountOTP, TelegramUser

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def handle_start(message: Message, tg_user: TelegramUser, state: FSMContext) -> None:
    """Handles the /start command."""
    await state.clear()  # Clear any previous state

    logger.info(f"TelegramUser {tg_user.telegram_id} started the bot.")

    # Check if account is linked
    if tg_user.user_id:
        user = tg_user.user
        assert user is not None  # user_id is set, so user must exist
        await message.answer(
            f"Welcome back, {user.first_name or user.username}!\n\n"
            f"I am your Revel companion.\n\n"
            f"By using this bot, you agree to our Terms and Conditions and Privacy Policy.\n"
            f"- use /toc to view Terms and Conditions\n"
            f"- use /privacy to view Privacy Policy\n",
            reply_markup=keyboards.get_main_menu_keyboard(),
        )
    else:
        await message.answer(
            "Welcome to Revel!\n\n"
            "To get started, link your Revel account using the /connect command.\n\n"
            "By using this bot, you agree to our Terms and Conditions and Privacy Policy.\n"
            "- use /toc to view Terms and Conditions\n"
            "- use /privacy to view Privacy Policy\n"
        )

    # Re-activate the user if they were deactivated or previously blocked the bot
    # (already handled in TelegramUserMiddleware)


@router.message(F.text == "🔙 Cancel")
@router.message(Command("cancel"))  # Add Command filter for /cancel
async def handle_cancel(message: Message, state: FSMContext) -> None:
    """Handles the generic 'Cancel' button press or /cancel command."""
    current_state = await state.get_state()
    if current_state is not None:
        await state.clear()
        await message.reply("Action cancelled.", reply_markup=keyboards.get_main_menu_keyboard())
    else:
        await message.reply("Nothing to cancel.", reply_markup=keyboards.get_main_menu_keyboard())


_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "strike", "del", "span", "code", "pre", "a"}

_TG_SPOILER_CLASS = "tg-spoiler"

_TAG_RE = re.compile(r"</?(\w+)([^>]*)>")


def sanitize_text(raw_html: str) -> str:
    """Sanitize a text to make it telegram-HTML compatible."""

    def _sanitize_tag(match: t.Any) -> str:
        tag = match.group(1)
        attrs = match.group(2)

        if tag not in _ALLOWED_TAGS:
            return ""  # Strip tag entirely

        if tag == "span":
            if f'class="{_TG_SPOILER_CLASS}"' not in attrs and f"class='{_TG_SPOILER_CLASS}'" not in attrs:
                return ""  # Only allow span if it’s a spoiler
        return t.cast(str, match.group(0))  # Safe, keep tag as-is

    return _TAG_RE.sub(_sanitize_tag, raw_html)


@router.message(Command("toc"))
async def handle_toc(message: Message, tg_user: TelegramUser) -> None:
    """Handles the /toc command to display Terms and Conditions."""
    logger.info(f"TelegramUser {tg_user.telegram_id} requested Terms and Conditions.")
    legal = await sync_to_async(Legal.get_solo)()
    toc = legal.terms_and_conditions or "Terms and Conditions are not available at the moment."
    sanitized_text = sanitize_text(toc)
    await message.answer(sanitized_text, parse_mode="HTML")


@router.message(Command("privacy"))
async def handle_privacy(message: Message, tg_user: TelegramUser) -> None:
    """Handles the /privacy command to display Privacy Policy."""
    logger.info(f"TelegramUser {tg_user.telegram_id} requested Privacy Policy.")
    legal = await sync_to_async(Legal.get_solo)()
    privacy = legal.privacy_policy or "Privacy Policy is not available at the moment."
    sanitized_text = sanitize_text(privacy)

    await message.answer(sanitized_text, parse_mode="HTML")


@router.message(Command("connect"))
async def handle_connect(message: Message, tg_user: TelegramUser) -> None:
    """Handles the /connect command to generate OTP for account linking."""
    logger.info(f"TelegramUser {tg_user.telegram_id} requested account linking.")

    # Check if already linked
    if tg_user.user_id:
        await message.answer("Your account is already linked!")
        return

    # Create or refresh OTP
    try:
        otp = await AccountOTP.objects.aget(tg_user=tg_user)
        if otp.is_expired():
            await otp.adelete()
            raise AccountOTP.DoesNotExist
    except AccountOTP.DoesNotExist:
        otp = await AccountOTP.objects.acreate(tg_user=tg_user)

    formatted_otp = " ".join(otp.otp[i : i + 3] for i in range(0, 9, 3))
    await message.answer(
        f"Here is your account linking code. Tap to copy:\n\n"
        f"<code>{formatted_otp}</code>\n\n"
        f"Enter this code in the Revel app to link your accounts.",
        parse_mode="HTML",
    )


@router.message(Command("unsubscribe"), flags={"requires_linked_user": True})
async def handle_unsubscribe(message: Message, tg_user: TelegramUser, user: RevelUser) -> None:
    """Handles the /unsubscribe command to turn off all Telegram notifications."""
    logger.info(f"TelegramUser {tg_user.telegram_id} requested unsubscribe.")

    # Get or create notification preferences and disable telegram channel
    from notifications.models import NotificationPreference

    prefs, created = await NotificationPreference.objects.aget_or_create(user=user)

    # Remove telegram from enabled channels if present
    await prefs.asave(update_fields=["enabled_channels", "updated_at"])
    logger.info(f"TelegramUser {tg_user.telegram_id} disabled Telegram notifications.")
    await message.answer(
        "✅ You have been unsubscribed from all Telegram notifications.\n\n"
        "You will no longer receive notification messages from Revel on Telegram.\n\n"
        "To re-enable notifications, use /preferences or update your settings in the Revel app."
    )
