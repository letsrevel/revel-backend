"""Google Wallet membership payload tests."""

import typing as t

import pytest
from django.core.files.base import ContentFile

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


@pytest.mark.django_db
def test_membership_object_carries_powered_by_link(member: OrganizationMember, settings: t.Any) -> None:
    """The membership card object carries the platform attribution link."""
    settings.GOOGLE_WALLET_ISSUER_ID = "1234"
    settings.GOOGLE_WALLET_CLASS_PREFIX = "revel"

    obj = build_membership_payload(member)["genericObjects"][0]
    assert obj["linksModuleData"] == {
        "uris": [{"id": "powered_by", "uri": "https://letsrevel.io", "description": "Powered by Revel"}]
    }


@pytest.mark.django_db
def test_membership_logo_uses_stable_indirection_url(
    member: OrganizationMember, png_bytes: bytes, settings: t.Any
) -> None:
    """The card logo embeds the stable org-logo API URL, not a raw media path.

    Raw media URLs die when the logo is replaced (uploads delete the old file),
    which would break cards already saved to wallets — same contract as ticket
    passes (#879).
    """
    settings.GOOGLE_WALLET_ISSUER_ID = "1234"
    settings.GOOGLE_WALLET_CLASS_PREFIX = "revel"
    settings.BASE_URL = "https://api.letsrevel.io"
    organization = member.organization
    organization.logo.save("logo.png", ContentFile(png_bytes), save=True)

    obj = build_membership_payload(member)["genericObjects"][0]
    assert obj["logo"]["sourceUri"]["uri"] == f"https://api.letsrevel.io/api/organizations/{organization.id}/logo"


@pytest.mark.django_db
def test_membership_logo_omitted_when_unset(member: OrganizationMember, settings: t.Any) -> None:
    settings.GOOGLE_WALLET_ISSUER_ID = "1234"
    settings.GOOGLE_WALLET_CLASS_PREFIX = "revel"
    settings.BASE_URL = "https://api.letsrevel.io"

    obj = build_membership_payload(member)["genericObjects"][0]
    assert "logo" not in obj


@pytest.mark.django_db
def test_membership_logo_omitted_for_non_https_base_url(
    member: OrganizationMember, png_bytes: bytes, settings: t.Any
) -> None:
    """Non-HTTPS indirection URLs (e.g. local dev) must be dropped, not embedded.

    Google's image fetcher rejects non-HTTPS URLs and voids the entire save link.
    """
    settings.GOOGLE_WALLET_ISSUER_ID = "1234"
    settings.GOOGLE_WALLET_CLASS_PREFIX = "revel"
    settings.BASE_URL = "http://localhost:8000"
    organization = member.organization
    organization.logo.save("logo.png", ContentFile(png_bytes), save=True)

    obj = build_membership_payload(member)["genericObjects"][0]
    assert "logo" not in obj
