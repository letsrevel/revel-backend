# src/telegram/service.py
"""Telegram service layer."""

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from ninja.errors import HttpError

from accounts.models import RevelUser
from telegram.bot import get_bot
from telegram.models import AccountOTP, TelegramUser
from telegram.tasks import send_message_task


def connect_accounts(user: RevelUser, otp: str) -> None:
    """Links Revel and Telegram accounts via OTP.

    Args:
        user: RevelUser to link.
        otp: 9-digit OTP code from Telegram bot.

    Raises:
        HttpError: 400 if account is already connected or OTP is invalid/expired.
    """
    # Check if user already has a linked Telegram account
    if TelegramUser.objects.filter(user=user).exists():
        raise HttpError(400, "Your account is already connected to Telegram.")

    # Find TelegramUser with matching valid OTP
    tg_user = (
        TelegramUser.objects.filter(
            account_otp__otp=otp, account_otp__expires_at__gt=timezone.now(), account_otp__used_at__isnull=True
        )
        .select_related("account_otp")
        .first()
    )

    if not tg_user:
        raise HttpError(400, "Invalid or expired OTP code.")

    # Link accounts
    tg_user.user = user
    with transaction.atomic():
        tg_user.save(update_fields=["user", "updated_at"])
        AccountOTP.objects.filter(otp=otp).update(used_at=timezone.now())

    # Send confirmation message to Telegram
    send_message_task.delay(
        tg_user.telegram_id,
        message=f"✅ Account linked successfully!\n\nWelcome to Revel, {user.first_name or user.username}!",
    )


def disconnect_account(user: RevelUser) -> None:
    """Disconnects Telegram account from Revel user.

    Args:
        user: RevelUser to disconnect from Telegram.

    Raises:
        HttpError: 400 if no Telegram account is linked.
    """
    tg_user = TelegramUser.objects.filter(user=user).first()

    if not tg_user:
        raise HttpError(400, "No Telegram account is linked to your account.")

    tg_user.user = None
    tg_user.save(update_fields=["user", "updated_at"])


def get_bot_name() -> str:
    """Get the bot name with caching.

    Retrieves the bot name from Telegram API and caches it for 24 hours
    to avoid unnecessary API calls.

    Returns:
        Bot name as string.
    """
    cache_key = "telegram:bot_name"

    cached_name = cache.get(cache_key)
    if cached_name is not None:
        return str(cached_name)

    # Fetch from Telegram API
    bot = get_bot()
    bot_name_obj = async_to_sync(bot.get_me)()
    bot_name: str = bot_name_obj.username or ""

    # Cache for 24 hours
    cache.set(cache_key, bot_name, timeout=86400)

    return bot_name
