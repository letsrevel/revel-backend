"""Org-admin lookup of an account by exact verified email."""

import typing as t
from uuid import UUID

from ninja import Schema

from events.models import OrganizationMember


class OrgUserLookupSchema(Schema):
    """A matched account and its standing in the organization. Deliberately carries no email or other personal data."""

    user_id: UUID
    display_name: str
    is_member: bool
    membership_status: OrganizationMember.MembershipStatus | None = None
    register_record_id: UUID | None = None
    pending_request_id: UUID | None = None


class OrgUserLookupNotFoundSchema(Schema):
    """The one answer for every non-match. Branch on ``code``; ``detail`` is translated."""

    detail: str
    code: t.Literal["user_not_found"]
