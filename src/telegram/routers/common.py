# src/telegram/handlers/common.py

import logging
import re
import typing as t

from aiogram import F, Router
from aiogram.filters import (
    Command,
    CommandStart,  # Added StateFilter
)
from aiogram.fsm.context import FSMContext
from aiogram.types import Message  # Added CallbackQuery, InaccessibleMessage
from asgiref.sync import sync_to_async
from django.conf import settings
from django.urls import reverse

from accounts.models import AccountOTP, RevelUser
from common.models import Legal
from telegram import keyboards
from telegram.models import TelegramUser

logger = logging.getLogger(__name__)
router = Router()


@router.message(CommandStart())
async def handle_start(message: Message, user: RevelUser, state: FSMContext) -> None:
    """Handles the /start command."""
    await state.clear()  # Clear any previous state

    # Safely access message.from_user.id
    tg_user_id = message.from_user.id if message.from_user else "unknown"
    logger.info(f"User {user.username} (TG ID: {tg_user_id}) started the bot.")
    await message.answer(
        f"Welcome, {user.first_name or user.username}!\n\n"
        f"I am Fabulor, your language learning story companion.\n\n"
        f"- use /preferences to set up your preferences\n"
        f"- use /generate to request a new story\n\n"
        f"By using this bot, you agree to our Terms and Conditions and Privacy Policy.\n"
        f"- use /toc to view Terms and Conditions\n"
        f"- use /privacy to view Privacy Policy\n",
        reply_markup=keyboards.get_main_menu_keyboard(),
    )
    # re-activate the user if they were deactivated or previously blocked the bot
    await TelegramUser.objects.filter(telegram_id=tg_user_id).aupdate(blocked_by_user=False, user_is_deactivated=False)


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
async def handle_toc(message: Message, user: RevelUser) -> None:
    """Handles the /toc command to display Terms and Conditions."""
    logger.info(f"User {user.username} requested Terms and Conditions.")
    legal = await sync_to_async(Legal.get_solo)()
    toc = legal.terms_and_conditions or "Terms and Conditions are not available at the moment."
    sanitized_text = sanitize_text(toc)
    await message.answer(sanitized_text, parse_mode="HTML")


@router.message(Command("privacy"))
async def handle_privacy(message: Message, user: RevelUser) -> None:
    """Handles the /privacy command to display Privacy Policy."""
    logger.info(f"User {user.username} requested Privacy Policy.")
    legal = await sync_to_async(Legal.get_solo)()
    privacy = legal.privacy_policy or "Privacy Policy is not available at the moment."
    sanitized_text = sanitize_text(privacy)

    await message.answer(sanitized_text, parse_mode="HTML")


@router.message(Command("weblogin"))
async def handle_weblogin(message: Message, user: RevelUser) -> None:
    """Handles the /weblogin command to display web login link and OTP."""
    logger.info(f"User {user.username} requested web login token and OTP.")
    otp, created = await AccountOTP.objects.aget_or_create(user=user)

    if not created and otp.is_expired():  # If OTP existed but was expired
        await otp.adelete()
        otp = await AccountOTP.objects.acreate(user=user)

    login_path = reverse("webapp:telegram_login")  # Get relative path
    # Construct the full URL. Ensure no double slashes if BASE_URL ends with / and login_path starts with /
    base_url = settings.BASE_URL.rstrip("/")
    relative_login_path = login_path.lstrip("/")
    login_url_with_token = f"{base_url}/{relative_login_path}?token={otp.token}"

    response_message = (
        f"Here is your One-Time Password for the web app. Tap the code to copy it:\n\n"
        f"<code>{otp.otp}</code>\n\n"
        f"Or, click this link to log in directly:\n\n"
        f"{login_url_with_token}"  # Telegram automatically makes URLs clickable
    )

    await message.answer(
        response_message,
        parse_mode="HTML",
        disable_web_page_preview=True,  # Good practice for login links
    )
