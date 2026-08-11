"""Tests for the OrganizationMember QR contract."""

import pytest

from accounts.models import RevelUser
from events.models import Organization, OrganizationMember


@pytest.mark.django_db
def test_qr_payload_is_prefixed_member_id(organization: Organization, member_user: RevelUser) -> None:
    member = OrganizationMember.objects.create(organization=organization, user=member_user)
    assert OrganizationMember.QR_PREFIX == "member:"
    assert member.qr_payload == f"member:{member.id}"
