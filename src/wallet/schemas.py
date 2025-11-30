"""Pydantic schemas for wallet pass API endpoints."""

from ninja import Schema
from pydantic import Field


class DeviceRegistrationPayload(Schema):
    """Payload sent by device when registering for pass updates."""

    pushToken: str = Field(..., description="Push token for sending notifications")


class SerialNumbersResponse(Schema):
    """Response containing list of updated pass serial numbers."""

    serialNumbers: list[str] = Field(default_factory=list)
    lastUpdated: str = Field(..., description="Unix timestamp of most recent update")


class LogEntry(Schema):
    """A single log entry from the device."""

    message: str


class LogPayload(Schema):
    """Payload for device error logging."""

    logs: list[str] = Field(default_factory=list)
