"""Organization brand colors on ticket/pass PDFs and the pass file cache (#1157)."""

import typing as t
from unittest.mock import Mock, patch

import pytest
from django.conf import settings
from django.template import engines
from django.template.loader import render_to_string

from events.models import Organization, OrganizationMember, Ticket
from events.service import ticket_file_service
from events.utils import create_membership_pdf, create_ticket_pdf
from events.utils.brand_theme import contrast_ratio, hex_to_rgb
from events.utils.pass_branding import get_pass_brand_colors

pytestmark = pytest.mark.django_db

BRANDED = {"primary_color": "#1E88E5", "accent_color": "#FFB300"}
REVEL_BANNER = "background: linear-gradient(to bottom, #8C3CDD, #E6332A)"


def _set_branding(organization: Organization, branding: t.Any) -> Organization:
    """Write ``branding`` around ``clean()`` (like a bad ``QuerySet.update`` would) and reload."""
    Organization.objects.filter(pk=organization.pk).update(branding=branding)
    organization.refresh_from_db()
    return organization


def _ticket_context(ticket: Ticket) -> dict[str, t.Any]:
    """Capture the context ``create_ticket_pdf`` hands to the ticket template."""
    with (
        patch("qrcode.QRCode") as mock_qr,
        patch("weasyprint.HTML") as mock_html,
        patch("events.utils.render_to_string") as mock_render,
    ):
        mock_qr.return_value = Mock()
        mock_html.return_value.write_pdf.return_value = b"fake-pdf"
        mock_render.return_value = "<html></html>"
        create_ticket_pdf(ticket)
    return t.cast(dict[str, t.Any], mock_render.call_args.kwargs["context"])


def _render_ticket(ticket: Ticket) -> str:
    return render_to_string("events/ticket.html", _ticket_context(ticket))


def _brand_ticket(ticket: Ticket, branding: t.Any = BRANDED) -> Ticket:
    """Set the ticket's organization branding and reload the ticket the way the controllers do."""
    _set_branding(ticket.event.organization, branding)
    return Ticket.objects.full().get(pk=ticket.pk)


class TestGetPassBrandColors:
    def test_none_without_branding(self, organization: Organization) -> None:
        assert get_pass_brand_colors(organization) is None

    def test_none_for_accent_only(self, organization: Organization) -> None:
        _set_branding(organization, {"accent_color": "#FFB300"})
        assert get_pass_brand_colors(organization) is None

    @pytest.mark.parametrize("bad", [{"primary_color": "blue"}, {"unknown": 1}, "not-a-dict", ["x"]])
    def test_none_for_invalid_stored_branding(self, organization: Organization, bad: t.Any) -> None:
        _set_branding(organization, bad)
        assert get_pass_brand_colors(organization) is None

    def test_branded_colors(self, organization: Organization) -> None:
        _set_branding(organization, BRANDED)
        brand = get_pass_brand_colors(organization)
        assert brand is not None
        assert brand.background == "#1E88E5"
        assert brand.foreground in {"#000000", "#FFFFFF"}

    @pytest.mark.parametrize("primary", ["#FFFF00", "#000000", "#FFFFFF", "#8C3CDD", "#FFB300"])
    def test_foreground_always_readable_on_background(self, organization: Organization, primary: str) -> None:
        _set_branding(organization, {"primary_color": primary})
        brand = get_pass_brand_colors(organization)
        assert brand is not None
        assert contrast_ratio(hex_to_rgb(brand.background), hex_to_rgb(brand.foreground)) >= 4.5

    def test_text_colors_legible_on_paper(self, organization: Organization) -> None:
        _set_branding(organization, {"primary_color": "#FFFF00", "accent_color": "#FFF59D"})
        brand = get_pass_brand_colors(organization)
        assert brand is not None
        assert contrast_ratio(hex_to_rgb(brand.text_accent), hex_to_rgb("#FFFFFF")) >= 4.5


class TestTicketPdfBranding:
    def test_context_has_no_brand_without_branding(self, ticket: Ticket) -> None:
        assert _ticket_context(ticket)["brand"] is None

    def test_unbranded_html_has_no_override_block(self, ticket: Ticket) -> None:
        """No branding: the override partial renders nothing, so the Revel default CSS is untouched."""
        html = _render_ticket(ticket)
        assert "#1E88E5" not in html
        assert ".banner, .cover-banner" not in html
        assert REVEL_BANNER in html

    def test_branded_html_overrides_surface_and_accents(self, ticket: Ticket) -> None:
        ticket = _brand_ticket(ticket)
        brand = get_pass_brand_colors(ticket.event.organization)
        assert brand is not None

        assert _ticket_context(ticket)["brand"] == brand
        html = _render_ticket(ticket)

        override = f".banner, .cover-banner {{ background: {brand.background}; }}"
        assert override in html
        assert f"color: {brand.text_accent}" in html
        # Every small-text rule, the 6.5pt stub eyebrow included, uses the AA ``text_accent``.
        assert f".stub .eyebrow, .stub .tid, .mid, .qr-instruction {{ color: {brand.text_accent}; }}" in html
        # The override block comes after the base chrome so it wins the cascade.
        assert html.index(override) > html.index(REVEL_BANNER)

    def test_branded_html_keeps_powered_by_lockup(self, ticket: Ticket) -> None:
        body = _render_ticket(_brand_ticket(ticket)).split("<body", 1)[1]
        assert "Powered by" in body
        assert "revel-mark.svg" in body
        assert 'style="color:#8C3CDD">r' in body
        assert 'style="color:#E6332A">l' in body

    def test_overlay_text_uses_foreground_only_without_cover_art(self, ticket: Ticket) -> None:
        ticket = _brand_ticket(ticket)
        brand = get_pass_brand_colors(ticket.event.organization)
        assert brand is not None
        ctx = _ticket_context(ticket)

        without_cover = render_to_string("events/ticket.html", {**ctx, "cover_art_url": None})
        assert f"background: none; color: {brand.foreground}" in without_cover

        with_cover = render_to_string("events/ticket.html", {**ctx, "cover_art_url": "data:image/png;base64,AAAA"})
        assert "background: none" not in with_cover

    def test_invalid_stored_branding_renders_like_unbranded(self, ticket: Ticket) -> None:
        ticket = _brand_ticket(ticket, {"primary_color": "nope"})
        assert _ticket_context(ticket)["brand"] is None
        assert ".banner, .cover-banner" not in _render_ticket(ticket)

    def test_real_weasyprint_renders_branded_pdf(self, ticket: Ticket) -> None:
        assert create_ticket_pdf(_brand_ticket(ticket))[:5] == b"%PDF-"


def _template_ctx(organization: Organization, brand: t.Any) -> dict[str, t.Any]:
    return {
        "brand": brand,
        "series_name": "S",
        "pass_name": "P",
        "organization_name": organization.name,
        "user_display_name": "U",
        "covered_events": [],
        "member_name": "M",
        "member_since": "today",
        "member_id": "1",
        "member_id_short": "1",
        "pass_id": "1",
        "pass_id_short": "1",
        "qr_code_base64": "AAAA",
        "font_dir": str(settings.BASE_DIR / "fonts"),
        "brand_mark": str(settings.BASE_DIR / "assets" / "brand" / "revel-mark.svg"),
    }


class TestMembershipAndSeriesTemplates:
    def test_membership_pdf_passes_brand_to_template(self, organization: Organization, member_user: t.Any) -> None:
        member = OrganizationMember.objects.create(organization=organization, user=member_user)
        _set_branding(organization, BRANDED)
        member = OrganizationMember.objects.select_related("organization", "user", "tier").get(pk=member.pk)

        with (
            patch("weasyprint.HTML") as mock_html,
            patch("events.utils.render_to_string", return_value="<html></html>") as mock_render,
        ):
            mock_html.return_value.write_pdf.return_value = b"%PDF-fake"
            create_membership_pdf(member)

        brand = mock_render.call_args.kwargs["context"]["brand"]
        assert brand is not None
        assert brand.background == "#1E88E5"

    def test_membership_card_branded_and_unbranded_css(self, organization: Organization) -> None:
        _set_branding(organization, BRANDED)
        brand = get_pass_brand_colors(organization)

        branded = render_to_string("events/membership_card.html", _template_ctx(organization, brand))
        assert ".banner, .cover-banner { background: #1E88E5; }" in branded
        assert "Powered by" in branded

        unbranded = render_to_string("events/membership_card.html", _template_ctx(organization, None))
        assert ".banner, .cover-banner" not in unbranded
        assert REVEL_BANNER in unbranded

    def test_series_pass_branded_and_unbranded_css(self, organization: Organization) -> None:
        _set_branding(organization, BRANDED)
        brand = get_pass_brand_colors(organization)

        branded = render_to_string("events/series_pass.html", _template_ctx(organization, brand))
        assert ".accent-bar { background: #1E88E5; }" in branded
        assert "Powered by" in branded

        unbranded = render_to_string("events/series_pass.html", _template_ctx(organization, None))
        assert ".accent-bar { background:" not in unbranded
        assert "linear-gradient(to right, #8C3CDD, #E6332A)" in unbranded


class TestUnbrandedOutputIsByteIdentical:
    """The override partial renders the empty string without a brand, so the HTML is exactly the old one."""

    INCLUDE = '{% include "events/_pass_brand_overrides.html" %}'

    def test_partial_renders_empty_without_brand(self) -> None:
        assert render_to_string("events/_pass_brand_overrides.html", {"brand": None}) == ""
        assert render_to_string("events/_pass_brand_overrides.html", {}) == ""

    @pytest.mark.parametrize("name", ["ticket", "series_pass", "membership_card"])
    def test_template_html_identical_without_the_include(
        self, name: str, organization: Organization, ticket: Ticket
    ) -> None:
        path = f"events/{name}.html"
        ctx = _ticket_context(ticket) if name == "ticket" else _template_ctx(organization, None)
        assert ctx["brand"] is None

        source = (settings.BASE_DIR / "events" / "templates" / path).read_text()
        assert source.count(self.INCLUDE) == 1
        without_include = engines["django"].from_string(source.replace(self.INCLUDE, "")).render(ctx)

        assert render_to_string(path, ctx) == without_include


class TestTicketFileCacheInvalidatesOnBranding:
    def test_branding_change_invalidates_cache(self, ticket: Ticket) -> None:
        ticket = Ticket.objects.full().get(pk=ticket.pk)
        unbranded_hash = ticket_file_service.compute_content_hash(ticket)
        Ticket.objects.filter(pk=ticket.pk).update(file_content_hash=unbranded_hash)
        assert ticket_file_service.is_cache_valid(Ticket.objects.full().get(pk=ticket.pk))

        ticket = _brand_ticket(ticket)
        assert not ticket_file_service.is_cache_valid(ticket)
        branded_hash = ticket_file_service.compute_content_hash(ticket)
        assert branded_hash != unbranded_hash

        ticket = _brand_ticket(ticket, {"primary_color": "#43A047"})
        assert ticket_file_service.compute_content_hash(ticket) != branded_hash

    def test_removing_branding_restores_original_hash(self, ticket: Ticket) -> None:
        ticket = Ticket.objects.full().get(pk=ticket.pk)
        unbranded_hash = ticket_file_service.compute_content_hash(ticket)
        ticket = _brand_ticket(ticket)
        ticket = _brand_ticket(ticket, {})
        assert ticket_file_service.compute_content_hash(ticket) == unbranded_hash
