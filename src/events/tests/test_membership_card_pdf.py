"""Membership card PDF generation tests.

Mirrors ``TestCreateSeriesPassPdf`` in ``test_series_pass/test_files.py``: a real-weasyprint
smoke test plus mocked QR-payload/context-data checks (mocking ``qrcode.QRCode``,
``weasyprint.HTML`` and ``events.utils.render_to_string``, as ``create_ticket_pdf``/
``create_series_pass_pdf`` are tested).
"""

import typing as t
from unittest.mock import MagicMock, Mock, patch

import pytest

from accounts.models import RevelUser
from events.models import MembershipTier, Organization, OrganizationMember
from events.utils import create_membership_pdf


@pytest.fixture
def member(organization: Organization, member_user: RevelUser) -> OrganizationMember:
    """Organization member with no tier, for membership card PDF tests."""
    return OrganizationMember.objects.create(organization=organization, user=member_user)


@pytest.fixture
def gold_tier(organization: Organization) -> MembershipTier:
    """A membership tier, for the tier-badge branch of the membership card."""
    return MembershipTier.objects.create(organization=organization, name="Gold")


@pytest.mark.django_db
def test_create_membership_pdf_returns_pdf_bytes(member: OrganizationMember) -> None:
    """Real weasyprint smoke test: the generated file starts with the PDF magic bytes."""
    pdf = create_membership_pdf(member)
    assert pdf[:5] == b"%PDF-"


@pytest.mark.django_db
@patch("qrcode.QRCode")
@patch("weasyprint.HTML")
@patch("events.utils.render_to_string")
def test_create_membership_pdf_qr_payload_is_member_prefixed(
    mock_render: Mock, mock_html: Mock, mock_qr: Mock, member: OrganizationMember
) -> None:
    """QR payload must be exactly ``member.qr_payload`` — the check-in contract."""
    mock_qr_instance = MagicMock()
    mock_qr.return_value = mock_qr_instance
    mock_qr_instance.make_image.return_value = MagicMock()
    mock_html_instance = MagicMock()
    mock_html.return_value = mock_html_instance
    mock_html_instance.write_pdf.return_value = b"%PDF-fake"
    mock_render.return_value = "<html></html>"

    create_membership_pdf(member)

    mock_qr_instance.add_data.assert_called_once_with(member.qr_payload)


@pytest.mark.django_db
@patch("qrcode.QRCode")
@patch("weasyprint.HTML")
@patch("events.utils.render_to_string")
def test_create_membership_pdf_context_data_no_tier(
    mock_render: Mock, mock_html: Mock, mock_qr: Mock, member: OrganizationMember
) -> None:
    """Rendered context must include org/member identity; tier_name is None without a tier."""
    mock_qr_instance = MagicMock()
    mock_qr.return_value = mock_qr_instance
    mock_qr_instance.make_image.return_value = MagicMock()
    mock_html_instance = MagicMock()
    mock_html.return_value = mock_html_instance
    mock_html_instance.write_pdf.return_value = b"%PDF-fake"
    mock_render.return_value = "<html></html>"

    result = create_membership_pdf(member)

    assert result == b"%PDF-fake"
    mock_render.assert_called_once()
    args, kwargs = mock_render.call_args
    assert args[0] == "events/membership_card.html"

    context = t.cast(dict[str, t.Any], kwargs["context"])
    assert context["organization_name"] == member.organization.name
    assert context["member_name"] == member.user.get_display_name()
    assert context["tier_name"] is None
    assert context["member_id"] == str(member.id)
    assert context["member_id_short"] == str(member.id)[:8].upper()


@pytest.mark.django_db
@patch("qrcode.QRCode")
@patch("weasyprint.HTML")
@patch("events.utils.render_to_string")
def test_create_membership_pdf_context_data_with_tier(
    mock_render: Mock,
    mock_html: Mock,
    mock_qr: Mock,
    member: OrganizationMember,
    gold_tier: MembershipTier,
) -> None:
    """tier_name reflects the member's tier when one is assigned."""
    member.tier = gold_tier
    member.save(update_fields=["tier"])

    mock_qr_instance = MagicMock()
    mock_qr.return_value = mock_qr_instance
    mock_qr_instance.make_image.return_value = MagicMock()
    mock_html_instance = MagicMock()
    mock_html.return_value = mock_html_instance
    mock_html_instance.write_pdf.return_value = b"%PDF-fake"
    mock_render.return_value = "<html></html>"

    create_membership_pdf(member)

    args, kwargs = mock_render.call_args
    context = t.cast(dict[str, t.Any], kwargs["context"])
    assert context["tier_name"] == gold_tier.name
