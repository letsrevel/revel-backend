"""Blacklist and whitelist schemas."""

from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import Field

from events import models


class BlacklistEntrySchema(ModelSchema):
    """Schema for retrieving a blacklist entry."""

    user_id: UUID | None = None
    user_display_name: str | None = None
    created_by_name: str | None = None

    class Meta:
        model = models.Blacklist
        fields = [
            "id",
            "email",
            "telegram_username",
            "phone_number",
            "first_name",
            "last_name",
            "preferred_name",
            "reason",
            "created_at",
        ]

    @staticmethod
    def resolve_user_id(obj: models.Blacklist) -> UUID | None:
        """Return the user ID if entry is linked to a user."""
        return obj.user_id

    @staticmethod
    def resolve_user_display_name(obj: models.Blacklist) -> str | None:
        """Return the display name of the linked user."""
        if obj.user:
            return obj.user.get_display_name()
        return None

    @staticmethod
    def resolve_created_by_name(obj: models.Blacklist) -> str | None:
        """Return the display name of who created the entry."""
        if obj.created_by:
            return obj.created_by.get_display_name()
        return None


class BlacklistCreateSchema(Schema):
    """Schema for creating a blacklist entry (manual mode).

    Provide at least one identifier (email, telegram, phone) or name.
    """

    user_id: UUID | None = Field(
        None,
        description="Quick mode: provide user ID to auto-populate all fields from user.",
    )
    email: str | None = Field(None, description="Email address")
    telegram_username: str | None = Field(
        None,
        description="Telegram username (with or without @ prefix)",
    )
    phone_number: str | None = Field(
        None,
        description="Phone number in E.164 format",
    )
    first_name: str | None = Field(None, max_length=150)
    last_name: str | None = Field(None, max_length=150)
    preferred_name: str | None = Field(None, max_length=150)
    reason: str = Field("", description="Reason for blacklisting")


class BlacklistUpdateSchema(Schema):
    """Schema for updating a blacklist entry.

    Only reason and name fields can be updated.
    Hard identifiers cannot be changed after creation.
    """

    reason: str | None = None
    first_name: str | None = Field(None, max_length=150)
    last_name: str | None = Field(None, max_length=150)
    preferred_name: str | None = Field(None, max_length=150)


class WhitelistRequestSchema(ModelSchema):
    """Schema for retrieving a whitelist request."""

    user_id: UUID
    user_display_name: str
    user_email: str
    matched_entries_count: int
    decided_by_name: str | None = None

    class Meta:
        model = models.WhitelistRequest
        fields = [
            "id",
            "message",
            "status",
            "created_at",
            "decided_at",
        ]

    @staticmethod
    def resolve_user_id(obj: models.WhitelistRequest) -> UUID:
        """Return the user ID of the requester."""
        return obj.user_id

    @staticmethod
    def resolve_user_display_name(obj: models.WhitelistRequest) -> str:
        """Return the display name of the requester."""
        return obj.user.get_display_name()

    @staticmethod
    def resolve_user_email(obj: models.WhitelistRequest) -> str:
        """Return the email of the requester."""
        return obj.user.email

    @staticmethod
    def resolve_matched_entries_count(obj: models.WhitelistRequest) -> int:
        """Return the count of matched blacklist entries."""
        return obj.matched_blacklist_entries.count()

    @staticmethod
    def resolve_decided_by_name(obj: models.WhitelistRequest) -> str | None:
        """Return the display name of who decided on the request."""
        if obj.decided_by:
            return obj.decided_by.get_display_name()
        return None


class WhitelistRequestCreateSchema(Schema):
    """Schema for users to request whitelisting."""

    message: str = Field("", description="Explanation for why you should be whitelisted")


class WhitelistEntrySchema(ModelSchema):
    """Schema for retrieving a whitelist entry (an APPROVED WhitelistRequest)."""

    user_id: UUID
    user_display_name: str
    user_email: str
    approved_by_name: str | None = None
    matched_entries_count: int

    class Meta:
        model = models.WhitelistRequest
        fields = ["id", "created_at", "decided_at"]

    @staticmethod
    def resolve_user_id(obj: models.WhitelistRequest) -> UUID:
        """Return the whitelisted user's ID."""
        return obj.user_id

    @staticmethod
    def resolve_user_display_name(obj: models.WhitelistRequest) -> str:
        """Return the display name of the whitelisted user."""
        return obj.user.get_display_name()

    @staticmethod
    def resolve_user_email(obj: models.WhitelistRequest) -> str:
        """Return the email of the whitelisted user."""
        return obj.user.email

    @staticmethod
    def resolve_approved_by_name(obj: models.WhitelistRequest) -> str | None:
        """Return the display name of who approved the whitelist entry."""
        if obj.decided_by:
            return obj.decided_by.get_display_name()
        return None

    @staticmethod
    def resolve_matched_entries_count(obj: models.WhitelistRequest) -> int:
        """Return the count of matched blacklist entries."""
        return obj.matched_blacklist_entries.count()
