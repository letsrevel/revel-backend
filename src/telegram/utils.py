# src/telegram/utils.py

import logging
from io import BytesIO

import qrcode
from aiogram.types import BufferedInputFile, ReplyMarkupUnion

logger = logging.getLogger(__name__)


async def send_telegram_message(
    telegram_id: int, *, message: str, reply_markup: ReplyMarkupUnion | None = None
) -> None:
    """Sends the story PDF and optional audio via Telegram."""
    from telegram.bot import get_bot  # avoid circular imports

    bot = get_bot()
    await bot.send_message(chat_id=telegram_id, text=message, reply_markup=reply_markup)
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
