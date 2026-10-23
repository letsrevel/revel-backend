"""Organization brand colors on Apple/Google wallet passes (#1157)."""

import io
import json
import re
import typing as t
import zipfile
from unittest.mock import MagicMock

import pytest
from PIL import Image

from events.models import Organization, OrganizationMember, Ticket
from events.utils.brand_theme import contrast_ratio, hex_to_rgb
from wallet.apple.formatting import (
    REVEL_THEME,
    get_theme_colors,
    get_theme_hex_background,
)
from wallet.apple.generator import ApplePassGenerator
from wallet.google.builder import build_membership_payload, build_series_pass_payload, build_ticket_payload

pytestmark = pytest.mark.django_db

BRANDED = {"primary_color": "#1E88E5", "accent_color": "#FFB300"}


def _set_branding(organization: Organization, branding: t.Any) -> None:
    """Write ``branding`` around ``clean()`` (like a bad ``QuerySet.update`` would)."""
    Organization.objects.filter(pk=organization.pk).update(branding=branding)
    organization.refresh_from_db()


def _pass_zip(pkpass: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(pkpass))


def _rgb(value: str) -> tuple[int, int, int]:
    inner = value.removeprefix("rgb(").removesuffix(")")
    r, g, b = (int(c) for c in inner.split(","))
    return r, g, b


@pytest.fixture
def generator(mock_signer: MagicMock, settings: t.Any) -> ApplePassGenerator:
    settings.APPLE_WALLET_PASS_TYPE_ID = "pass.com.test.app"
    settings.APPLE_WALLET_TEAM_ID = "TEAM123"
    return ApplePassGenerator(signer=mock_signer)


class TestFormattingHelpers:
    def test_unbranded_returns_revel_theme_object(self, organization: Organization) -> None:
        assert get_theme_colors(organization) is REVEL_THEME
        assert get_theme_colors(None) is REVEL_THEME
        assert get_theme_colors() is REVEL_THEME
        assert get_theme_hex_background(organization) == "#8C3CDD"

    def test_invalid_branding_falls_back_to_revel(self, organization: Organization) -> None:
        _set_branding(organization, {"primary_color": "not-a-color"})
        assert get_theme_colors(organization) is REVEL_THEME
        assert get_theme_hex_background(organization) == "#8C3CDD"

    def test_branded_colors(self, organization: Organization) -> None:
        _set_branding(organization, BRANDED)
        colors = get_theme_colors(organization)
        assert colors.background == "rgb(30, 136, 229)"
        assert colors.label == colors.foreground
        assert contrast_ratio(_rgb(colors.background), _rgb(colors.foreground)) >= 4.5
        assert get_theme_hex_background(organization) == "#1E88E5"

    @pytest.mark.parametrize("primary", ["#FFFF00", "#000000", "#FFFFFF", "#FFB300"])
    def test_foreground_readable_on_any_primary(self, organization: Organization, primary: str) -> None:
        _set_branding(organization, {"primary_color": primary})
        colors = get_theme_colors(organization)
        assert contrast_ratio(_rgb(colors.background), _rgb(colors.foreground)) >= 4.5
        google_rgb = hex_to_rgb(get_theme_hex_background(organization))
        assert google_rgb == _rgb(colors.background)
        assert contrast_ratio(google_rgb, _rgb(colors.foreground)) >= 4.5


class TestApplePassBranding:
    def test_unbranded_ticket_pass_unchanged(self, generator: ApplePassGenerator, ticket: Ticket) -> None:
        pass_json = json.loads(_pass_zip(generator.generate_pass(ticket)).read("pass.json"))
        assert pass_json["backgroundColor"] == REVEL_THEME.background
        assert pass_json["foregroundColor"] == REVEL_THEME.foreground
        assert pass_json["labelColor"] == REVEL_THEME.label

    def test_unbranded_background_is_gradient(self, generator: ApplePassGenerator, ticket: Ticket) -> None:
        bg = Image.open(io.BytesIO(_pass_zip(generator.generate_pass(ticket)).read("background.png"))).convert("RGB")
        assert bg.getpixel((90, 0)) != bg.getpixel((90, 219))

    def test_branded_ticket_pass(
        self, generator: ApplePassGenerator, ticket: Ticket, organization: Organization
    ) -> None:
        _set_branding(organization, BRANDED)
        ticket = Ticket.objects.full().get(pk=ticket.pk)
        zf = _pass_zip(generator.generate_pass(ticket))
        pass_json = json.loads(zf.read("pass.json"))

        assert pass_json["backgroundColor"] == "rgb(30, 136, 229)"
        assert pass_json["foregroundColor"] == pass_json["labelColor"]
        assert contrast_ratio(_rgb(pass_json["backgroundColor"]), _rgb(pass_json["foregroundColor"])) >= 4.5

        # The blurred background image is a solid brand fill, not the Revel gradient.
        for name in ("background.png", "background@2x.png", "background@3x.png"):
            img = Image.open(io.BytesIO(zf.read(name))).convert("RGB")
            assert img.getpixel((0, 0)) == (30, 136, 229)
            assert img.getpixel((img.width - 1, img.height - 1)) == (30, 136, 229)
        icon = Image.open(io.BytesIO(zf.read("icon.png"))).convert("RGB")
        assert icon.getpixel((0, 0)) == (30, 136, 229)

    def test_branded_ticket_pass_keeps_powered_by(
        self, generator: ApplePassGenerator, ticket: Ticket, organization: Organization
    ) -> None:
        _set_branding(organization, BRANDED)
        ticket = Ticket.objects.full().get(pk=ticket.pk)
        pass_json = json.loads(_pass_zip(generator.generate_pass(ticket)).read("pass.json"))
        assert any(f["key"] == "powered_by" for f in pass_json["eventTicket"]["backFields"])

    def test_invalid_branding_ticket_pass_matches_unbranded(
        self, generator: ApplePassGenerator, ticket: Ticket, organization: Organization
    ) -> None:
        _set_branding(organization, {"primary_color": "bogus", "extra": 1})
        ticket = Ticket.objects.full().get(pk=ticket.pk)
        pass_json = json.loads(_pass_zip(generator.generate_pass(ticket)).read("pass.json"))
        assert pass_json["backgroundColor"] == REVEL_THEME.background
        assert pass_json["labelColor"] == REVEL_THEME.label

    def test_membership_pass_branded_and_unbranded(
        self, generator: ApplePassGenerator, member: OrganizationMember, organization: Organization
    ) -> None:
        plain = json.loads(_pass_zip(generator.generate_membership_pass(member)).read("pass.json"))
        assert plain["backgroundColor"] == REVEL_THEME.background

        _set_branding(organization, BRANDED)
        member = OrganizationMember.objects.select_related("organization", "tier", "user").get(pk=member.pk)
        branded = json.loads(_pass_zip(generator.generate_membership_pass(member)).read("pass.json"))
        assert branded["backgroundColor"] == "rgb(30, 136, 229)"

    def test_series_pass_branded(
        self, generator: ApplePassGenerator, held_pass: t.Any, organization: Organization
    ) -> None:
        _set_branding(organization, BRANDED)
        held_pass = (
            type(held_pass).objects.select_related("series_pass__event_series__organization").get(pk=held_pass.pk)
        )
        pass_json = json.loads(_pass_zip(generator.generate_series_pass(held_pass)).read("pass.json"))
        assert pass_json["backgroundColor"] == "rgb(30, 136, 229)"


class TestGoogleWalletBranding:
    def test_unbranded_ticket_class_unchanged(self, google_wallet_configured_settings: None, ticket: Ticket) -> None:
        cls = build_ticket_payload(ticket)["eventTicketClasses"][0]
        assert cls["hexBackgroundColor"] == "#8C3CDD"

    def test_branded_ticket_class(
        self, google_wallet_configured_settings: None, ticket: Ticket, organization: Organization
    ) -> None:
        _set_branding(organization, BRANDED)
        ticket = Ticket.objects.full().get(pk=ticket.pk)
        payload = build_ticket_payload(ticket)
        assert payload["eventTicketClasses"][0]["hexBackgroundColor"] == "#1E88E5"
        # The Powered by attribution link stays.
        assert payload["eventTicketObjects"][0]["linksModuleData"]

    def test_invalid_branding_falls_back(
        self, google_wallet_configured_settings: None, ticket: Ticket, organization: Organization
    ) -> None:
        _set_branding(organization, ["nonsense"])
        ticket = Ticket.objects.full().get(pk=ticket.pk)
        assert build_ticket_payload(ticket)["eventTicketClasses"][0]["hexBackgroundColor"] == "#8C3CDD"

    def test_membership_object_branded_and_unbranded(
        self, google_wallet_configured_settings: None, member: OrganizationMember, organization: Organization
    ) -> None:
        assert build_membership_payload(member)["genericObjects"][0]["hexBackgroundColor"] == "#8C3CDD"
        _set_branding(organization, BRANDED)
        member = OrganizationMember.objects.select_related("organization", "tier", "user").get(pk=member.pk)
        assert build_membership_payload(member)["genericObjects"][0]["hexBackgroundColor"] == "#1E88E5"

    def test_series_pass_class_branded(
        self, google_wallet_configured_settings: None, held_pass: t.Any, organization: Organization
    ) -> None:
        assert build_series_pass_payload(held_pass)["eventTicketClasses"][0]["hexBackgroundColor"] == "#8C3CDD"
        _set_branding(organization, BRANDED)
        held_pass = (
            type(held_pass).objects.select_related("series_pass__event_series__organization").get(pk=held_pass.pk)
        )
        assert build_series_pass_payload(held_pass)["eventTicketClasses"][0]["hexBackgroundColor"] == "#1E88E5"


_SUFFIX = re.compile(r"-[0-9a-f]{8}$")
_GOOGLE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _reload_ticket(ticket: Ticket) -> Ticket:
    return Ticket.objects.full().get(pk=ticket.pk)


class TestGoogleWalletBrandFingerprintIds:
    """Google no-ops a save JWT whose class/object ID already exists, so branding is baked into the IDs."""

    def test_unbranded_ids_have_no_suffix(
        self,
        google_wallet_configured_settings: None,
        ticket: Ticket,
        member: OrganizationMember,
        held_pass: t.Any,
    ) -> None:
        event_id = ticket.event_id
        assert (
            build_ticket_payload(ticket)["eventTicketClasses"][0]["id"] == f"3388000000012345678.test.event.{event_id}"
        )
        series_id = held_pass.series_pass_id
        assert (
            build_series_pass_payload(held_pass)["eventTicketClasses"][0]["id"]
            == f"3388000000012345678.test.series.{series_id}"
        )
        obj_id = build_membership_payload(member)["genericObjects"][0]["id"]
        assert obj_id == f"3388000000012345678.test.membercard.{member.id}-base"

    def test_branded_ticket_class_id_carries_suffix(
        self, google_wallet_configured_settings: None, ticket: Ticket, organization: Organization
    ) -> None:
        _set_branding(organization, BRANDED)
        payload = build_ticket_payload(_reload_ticket(ticket))
        cls_id = payload["eventTicketClasses"][0]["id"]
        prefix = f"3388000000012345678.test.event.{ticket.event_id}"
        assert cls_id.startswith(prefix)
        assert _SUFFIX.fullmatch(cls_id.removeprefix(prefix))
        assert _GOOGLE_ID.fullmatch(cls_id)
        assert payload["eventTicketObjects"][0]["classId"] == cls_id

    def test_suffix_is_stable_and_follows_the_colors(
        self, google_wallet_configured_settings: None, ticket: Ticket, organization: Organization
    ) -> None:
        def class_id() -> str:
            return str(build_ticket_payload(_reload_ticket(ticket))["eventTicketClasses"][0]["id"])

        unbranded = class_id()
        _set_branding(organization, BRANDED)
        first = class_id()
        assert class_id() == first
        # The accent is not a pass color, so it does not move the ID.
        _set_branding(organization, {**BRANDED, "accent_color": "#43A047"})
        assert class_id() == first
        _set_branding(organization, {"primary_color": "#43A047"})
        changed = class_id()
        assert changed != first
        _set_branding(organization, {})
        assert class_id() == unbranded

    def test_series_class_id_carries_suffix(
        self, google_wallet_configured_settings: None, held_pass: t.Any, organization: Organization
    ) -> None:
        unbranded = build_series_pass_payload(held_pass)["eventTicketClasses"][0]["id"]
        _set_branding(organization, BRANDED)
        held_pass = (
            type(held_pass).objects.select_related("series_pass__event_series__organization").get(pk=held_pass.pk)
        )
        branded = build_series_pass_payload(held_pass)["eventTicketClasses"][0]["id"]
        assert branded.startswith(unbranded)
        assert _SUFFIX.fullmatch(branded.removeprefix(unbranded))
        assert _GOOGLE_ID.fullmatch(branded)

    def test_membership_object_id_carries_suffix(
        self, google_wallet_configured_settings: None, member: OrganizationMember, organization: Organization
    ) -> None:
        def object_id() -> str:
            fresh = OrganizationMember.objects.select_related("organization", "tier", "user").get(pk=member.pk)
            return str(build_membership_payload(fresh)["genericObjects"][0]["id"])

        unbranded = object_id()
        _set_branding(organization, BRANDED)
        first = object_id()
        assert first.startswith(unbranded)
        assert _SUFFIX.fullmatch(first.removeprefix(unbranded))
        assert _GOOGLE_ID.fullmatch(first)
        assert object_id() == first
        _set_branding(organization, {"primary_color": "#43A047"})
        assert object_id() != first
