"""Personal membership invitations sent by org staff."""

import typing as t
from uuid import UUID

from ninja import Field, Schema
from pydantic import AwareDatetime


class MemberInvitationCreateSchema(Schema):
    user_id: UUID
    tier_id: UUID


class MemberInvitationSchema(Schema):
    """A pending invitation. Deliberately carries no email, phone or legal name of the invitee."""

    id: str
    invitee_id: UUID
    invitee_display_name: str
    tier_id: UUID = Field(alias="membership_tier_id")
    tier_name: str = Field(alias="membership_tier.name")
    expires_at: AwareDatetime
    created_at: AwareDatetime

    @staticmethod
    def resolve_invitee_display_name(obj: t.Any) -> str:
        """Preferred or full name; never fall back to ``username`` (it is the email address)."""
        return str(obj.invitee.preferred_name or obj.invitee.get_full_name())
