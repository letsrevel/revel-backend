"""Apple Wallet membership pass generation tests."""

import json
import typing as t
import zipfile
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from events.models import MembershipTier, Organization, OrganizationMember
from wallet.apple.generator import ApplePassGenerator


def _extract_pass_json(pkpass_bytes: bytes) -> dict[str, t.Any]:
    with zipfile.ZipFile(BytesIO(pkpass_bytes)) as zf:
        return t.cast(dict[str, t.Any], json.loads(zf.read("pass.json")))


@pytest.fixture
def generator(mock_signer: MagicMock) -> ApplePassGenerator:
    """Apple pass generator with a mocked signer."""
    return ApplePassGenerator(signer=mock_signer)


@pytest.mark.django_db
def test_membership_pass_structure(
    generator: ApplePassGenerator, organization: Organization, member: OrganizationMember
) -> None:
    pkpass = generator.generate_membership_pass(member)
    pass_json = _extract_pass_json(pkpass)

    assert pass_json["serialNumber"] == str(member.id)
    assert pass_json["barcodes"][0]["message"] == f"member:{member.id}"
    assert "generic" in pass_json
    assert "eventTicket" not in pass_json
    assert "expirationDate" not in pass_json
    assert "relevantDate" not in pass_json
    # Face: org as primary label, member name as value
    primary = pass_json["generic"]["primaryFields"][0]
    assert primary["label"] == organization.name
    assert primary["value"] == member.user.get_display_name()


@pytest.mark.django_db
def test_membership_pass_tier_shown_and_omitted(
    generator: ApplePassGenerator, member: OrganizationMember, tier: MembershipTier
) -> None:
    member.tier = tier
    member.save(update_fields=["tier"])
    with_tier = _extract_pass_json(generator.generate_membership_pass(member))
    keys = [f["key"] for f in with_tier["generic"]["secondaryFields"]]
    assert "tier" in keys

    member.tier = None
    member.save(update_fields=["tier"])
    without = _extract_pass_json(generator.generate_membership_pass(member))
    keys = [f["key"] for f in without["generic"]["secondaryFields"]]
    assert "tier" not in keys
