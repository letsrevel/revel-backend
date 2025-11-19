# src/telegram/utils.py

from io import BytesIO

import qrcode
import structlog
from aiogram.types import BufferedInputFile, ReplyMarkupUnion
from aiogram.types import User as AiogramUser

from telegram.models import TelegramUser

logger = structlog.get_logger(__name__)


async def get_or_create_tg_user(aiogram_user: AiogramUser) -> TelegramUser:
    """Get or create TelegramUser (without creating RevelUser).

    Args:
        aiogram_user: Aiogram User object from incoming update.

    Returns:
        TelegramUser instance with prefetched user relationship.
    """
    try:
        tg_user = await TelegramUser.objects.select_related("user").aget(telegram_id=aiogram_user.id)
        # Update username if it changed
        if aiogram_user.username and tg_user.telegram_username != aiogram_user.username:
            tg_user.telegram_username = aiogram_user.username
            await tg_user.asave(update_fields=["telegram_username", "updated_at"])
        return tg_user
    except TelegramUser.DoesNotExist:
        tg_user = await TelegramUser.objects.acreate(
            user=None,
            telegram_id=aiogram_user.id,
            telegram_username=aiogram_user.username,
        )
        logger.info("created_telegram_user", telegram_id=aiogram_user.id)
        return tg_user


async def send_telegram_message(
    telegram_id: int,
    *,
    message: str,
    reply_markup: ReplyMarkupUnion | None = None,
    photo: BufferedInputFile | None = None,
) -> None:
    """Sends a message via Telegram with HTML parse mode.

    Args:
        telegram_id: Telegram user ID
        message: Message text (HTML formatted)
        reply_markup: Optional keyboard markup
        photo: Optional photo attachment (BufferedInputFile)
    """
    from aiogram.enums import ParseMode

    from telegram.bot import get_bot  # avoid circular imports

    bot = get_bot()

    if photo:
        # Send photo with caption
        await bot.send_photo(
            chat_id=telegram_id, photo=photo, caption=message, reply_markup=reply_markup, parse_mode=ParseMode.HTML
        )
    else:
        # Send text message
        await bot.send_message(chat_id=telegram_id, text=message, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    await bot.session.close()


def generate_qr_code(data: str) -> BufferedInputFile:
    """Generates a QR code image from the given data and returns it as a buffer."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    buffer = BytesIO()
    img.save(buffer, "PNG")
    buffer.seek(0)  # Rewind the buffer to the beginning
    return BufferedInputFile(buffer.read(), filename=f"{data}.png")
