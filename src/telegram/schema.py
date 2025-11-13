# src/telegram/schema.py
"""Telegram API schemas."""

from ninja import Schema
from pydantic import Field


class TelegramOTPSchema(Schema):
    """Schema for OTP input from user."""

    otp: str = Field(
        ..., min_length=9, max_length=11, description="9-digit OTP with optional spaces", examples=["123 456 789"]
    )

    def cleaned_otp(self) -> str:
        """Remove spaces from OTP for validation."""
        return self.otp.replace(" ", "")


class TelegramLinkStatusSchema(Schema):
    """Schema for telegram link status response."""

    connected: bool = Field(..., description="Whether Telegram is linked to Revel account")
    telegram_username: str | None = Field(None, description="Telegram username if connected")


class BotNameSchema(Schema):
    """Schema for bot name response."""

    botname: str = Field(..., description="Name of the Telegram bot")
