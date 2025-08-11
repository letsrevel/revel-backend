# src/telegram/tests/test_utils.py
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.types import BufferedInputFile

from telegram.utils import generate_qr_code, send_telegram_message

pytestmark = pytest.mark.asyncio


async def test_send_telegram_message() -> None:
    """Test the send_telegram_message utility function."""
    with patch("telegram.bot.get_bot") as mock_get_bot:
        mock_bot = AsyncMock()
        mock_get_bot.return_value = mock_bot

        telegram_id = 12345
        message_text = "Test message"

        await send_telegram_message(telegram_id, message=message_text)

        mock_get_bot.assert_called_once()
        mock_bot.send_message.assert_awaited_once_with(chat_id=telegram_id, text=message_text, reply_markup=None)
        mock_bot.session.close.assert_awaited_once()


def test_generate_qr_code() -> None:
    """Test the QR code generation utility."""
    data = "test_qr_code_data"
    qr_code_file = generate_qr_code(data)

    assert isinstance(qr_code_file, BufferedInputFile)
    assert qr_code_file.filename == f"{data}.png"
    # Check if the file content is a valid PNG header
    assert qr_code_file.data.startswith(b"\x89PNG\r\n\x1a\n")
