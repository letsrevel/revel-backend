"""Schemas for the membership register API (#1319)."""

import typing as t
from datetime import date
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import AwareDatetime, EmailStr, Field, field_validator

from events.models import MembershipRecord, MembershipRecordEndedReason, MembershipRecordKind, MembershipRecordSource

_FULL_NAME_MAX = 255


def _clean_full_name(value: str | None) -> str | None:
    """Strip and reject blank names (``None`` passes through; callers decide whether it is allowed)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("Name cannot be blank.")
    return value


class MembershipRecordSchema(ModelSchema):
    """A register entry. ``source == backfill`` means the dates are approximate."""

    user_id: UUID | None = None
    tier_id: UUID | None = None
    event_id: UUID | None = None
    ticket_id: UUID | None = None
    kind: MembershipRecordKind
    source: MembershipRecordSource
    ended_reason: MembershipRecordEndedReason | None = None
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None
    accepted_at: AwareDatetime | None = None
    created_at: AwareDatetime
    has_account: bool

    class Meta:
        model = MembershipRecord
        fields = ["id", "full_name", "email", "tier_name", "questionnaire_name"]

    @staticmethod
    def resolve_has_account(obj: MembershipRecord) -> bool:
        """Whether the person still has an account."""
        return obj.user_id is not None


class MembershipRecordCreateSchema(Schema):
    """Manually add an account-less register entry (linking to an account is a separate step)."""

    full_name: str = Field(..., min_length=1, max_length=_FULL_NAME_MAX)
    email: EmailStr | None = None
    tier_id: UUID
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return t.cast(str, _clean_full_name(value))


class MembershipRecordUpdateSchema(Schema):
    """Partial update of a register entry. Explicit null is rejected for required columns."""

    full_name: str | None = Field(default=None, max_length=_FULL_NAME_MAX)
    email: EmailStr | None = None
    tier_id: UUID | None = None
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None

    @field_validator("full_name", "tier_id", "valid_from")
    @classmethod
    def _not_null(cls, value: t.Any) -> t.Any:
        if value is None:
            raise ValueError("This field cannot be null.")
        return value

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str | None:
        return _clean_full_name(value)


class MembershipRecordEndSchema(Schema):
    """End a register entry."""

    reason: MembershipRecordEndedReason
    valid_until: AwareDatetime | None = None

    @field_validator("reason")
    @classmethod
    def _allowed_reason(cls, value: MembershipRecordEndedReason) -> MembershipRecordEndedReason:
        if value not in (MembershipRecord.EndedReason.CANCELLED, MembershipRecord.EndedReason.REMOVED):
            raise ValueError("Reason must be 'cancelled' or 'removed'.")
        return value


class MemberRegisterExportRequestSchema(Schema):
    """Export the register, optionally only the entries active on a calendar day in the organization's timezone."""

    active_on: date | None = Field(None, lt=date.max)  # the next day must exist
