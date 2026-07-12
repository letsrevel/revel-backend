"""Organization service.

This package groups the organization app's service functions by concern
(lifecycle, membership, tokens, contact). Every public symbol is re-exported
here so the historical ``from events.service import organization_service``
import path (and ``organization_service.<name>`` access) keeps working.
"""

from events.service.organization_service.contact import (
    create_and_send_contact_email_verification,
    create_contact_message,
    update_contact_email,
    validate_contact_method,
    verify_contact_email,
)
from events.service.organization_service.lifecycle import (
    REVENUE_CADENCE_OWNER_ONLY_MESSAGE,
    create_organization,
    update_organization,
)
from events.service.organization_service.membership import (
    add_member,
    add_staff,
    approve_membership_request,
    create_membership_request,
    reject_membership_request,
    remove_member,
    remove_staff,
    reorder_membership_tiers,
    update_member,
    update_staff_permissions,
)
from events.service.organization_service.tokens import (
    GRANT_INVARIANT_MESSAGE,
    MEMBERSHIP_TIER_REQUIRED_MESSAGE,
    STAFF_GRANT_FORBIDDEN_MESSAGE,
    OrgTokenRejection,
    claim_invitation,
    create_organization_token,
    create_organization_token_from_payload,
    delete_organization_token,
    get_org_token_rejection_reason,
    get_organization_token,
    update_organization_token,
)

__all__ = [
    "GRANT_INVARIANT_MESSAGE",
    "MEMBERSHIP_TIER_REQUIRED_MESSAGE",
    "REVENUE_CADENCE_OWNER_ONLY_MESSAGE",
    "STAFF_GRANT_FORBIDDEN_MESSAGE",
    "OrgTokenRejection",
    "add_member",
    "add_staff",
    "approve_membership_request",
    "claim_invitation",
    "create_and_send_contact_email_verification",
    "create_contact_message",
    "create_membership_request",
    "create_organization",
    "create_organization_token",
    "create_organization_token_from_payload",
    "delete_organization_token",
    "get_org_token_rejection_reason",
    "get_organization_token",
    "reject_membership_request",
    "remove_member",
    "remove_staff",
    "reorder_membership_tiers",
    "update_contact_email",
    "update_member",
    "update_organization",
    "update_organization_token",
    "update_staff_permissions",
    "validate_contact_method",
    "verify_contact_email",
]
