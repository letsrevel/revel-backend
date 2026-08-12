"""Apple Wallet membership pass generation tests."""

import json
import typing as t
import zipfile
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from events.models import MembershipTier, Organization, OrganizationMember
from wallet.apple.generator import ApplePassGenerator, ApplePassGeneratorError
from wallet.apple.signer import ApplePassSignerError


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


@pytest.mark.django_db
def test_membership_pass_back_fields_carry_powered_by(
    generator: ApplePassGenerator, member: OrganizationMember
) -> None:
    """The membership card carries the platform attribution back field."""
    pass_json = _extract_pass_json(generator.generate_membership_pass(member))
    back = pass_json["generic"]["backFields"]
    assert back[-1] == {
        "key": "powered_by",
        "label": "Powered by",
        "value": "Revel — https://letsrevel.io",
    }


@pytest.mark.django_db
def test_membership_pass_raises_on_signer_error(member: OrganizationMember) -> None:
    """ApplePassSignerError must propagate unchanged (mirrors the ticket generator contract)."""
    mock_signer = MagicMock()
    mock_signer.create_manifest.side_effect = ApplePassSignerError("Signing failed")
    generator = ApplePassGenerator(signer=mock_signer)

    with pytest.raises(ApplePassSignerError):
        generator.generate_membership_pass(member)


@pytest.mark.django_db
def test_membership_pass_raises_generator_error_on_failure(
    generator: ApplePassGenerator, member: OrganizationMember
) -> None:
    """General failures surface as ApplePassGeneratorError."""
    with patch.object(generator, "_build_membership_pass_data", side_effect=Exception("Build error")):
        with pytest.raises(ApplePassGeneratorError, match="Failed to generate pass"):
            generator.generate_membership_pass(member)
