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
from telegram.signals import telegram_account_linked, telegram_account_unlinked
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

        # Link any existing blacklist entries that match this telegram username
        if tg_user.telegram_username:
            from events.service.blacklist_service import link_blacklist_entries_by_telegram

            link_blacklist_entries_by_telegram(user, tg_user.telegram_username)

        transaction.on_commit(
            lambda: telegram_account_linked.send(sender=TelegramUser, user=user, telegram_user=tg_user)
        )

    # Send confirmation message to Telegram
    send_message_task.delay(
        tg_user.telegram_id,
        message=f"✅ Account linked successfully!\n\nWelcome to Revel, {user.display_name}!",
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

    # Capture `user` before clearing tg_user.user, since the on_commit lambda
    # will see tg_user.user as None by the time it fires.
    tg_user.user = None
    with transaction.atomic():
        tg_user.save(update_fields=["user", "updated_at"])
        transaction.on_commit(
            lambda: telegram_account_unlinked.send(sender=TelegramUser, user=user, telegram_user=tg_user)
        )


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
