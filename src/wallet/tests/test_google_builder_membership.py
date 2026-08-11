"""Google Wallet membership payload tests."""

import typing as t

import pytest

from events.models import MembershipTier, OrganizationMember
from wallet.google.builder import build_membership_payload


@pytest.mark.django_db
def test_membership_object_id_carries_tier(member: OrganizationMember, tier: MembershipTier, settings: t.Any) -> None:
    settings.GOOGLE_WALLET_ISSUER_ID = "1234"
    settings.GOOGLE_WALLET_CLASS_PREFIX = "revel"
    member.tier = tier
    member.save(update_fields=["tier"])

    payload = build_membership_payload(member)
    obj = payload["genericObjects"][0]
    cls = payload["genericClasses"][0]

    assert obj["id"] == f"1234.revel.membercard.{member.id}-{tier.id}"
    assert cls["id"] == f"1234.revel.memberorg.{member.organization_id}"
    assert obj["classId"] == cls["id"]
    # Barcode is ALWAYS the stable member payload, never the tier-suffixed object id
    assert obj["barcode"] == {"type": "QR_CODE", "value": f"member:{member.id}"}
    assert obj["cardTitle"]["defaultValue"]["value"] == member.organization.name
    assert obj["header"]["defaultValue"]["value"] == member.user.get_display_name()
    assert obj["subheader"]["defaultValue"]["value"] == tier.name
    assert "validTimeInterval" not in obj


@pytest.mark.django_db
def test_membership_object_id_base_when_tierless(member: OrganizationMember, settings: t.Any) -> None:
    settings.GOOGLE_WALLET_ISSUER_ID = "1234"
    settings.GOOGLE_WALLET_CLASS_PREFIX = "revel"
    member.tier = None
    member.save(update_fields=["tier"])

    obj = build_membership_payload(member)["genericObjects"][0]
    assert obj["id"] == f"1234.revel.membercard.{member.id}-base"
    assert "subheader" not in obj
