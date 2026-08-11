# src/events/tests/test_controllers/test_public_asset_images.py

"""Tests for the public org-logo / event-cover-art image endpoints.

These endpoints back the stable image URLs embedded in Google Wallet save
links: they must keep resolving to *an* image (current file or placeholder)
for any existing org/event, with no auth, forever — Google's image fetcher
errors otherwise and the save link dies.
"""

import pytest
from django.core.files.base import ContentFile
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Organization

pytestmark = pytest.mark.django_db

PNG_MAGIC = b"\x89PNG"


class TestOrganizationLogo:
    def test_serves_logo_bytes(self, client: Client, organization: Organization, png_bytes: bytes) -> None:
        """The current logo file is served with cache headers."""
        organization.logo.save("logo.png", ContentFile(png_bytes), save=True)
        url = reverse("api:organization_logo", kwargs={"organization_id": organization.id})

        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response.content == png_bytes
        assert "public" in response["Cache-Control"]

    def test_prefers_thumbnail_over_logo(self, client: Client, organization: Organization, png_bytes: bytes) -> None:
        """logo_thumbnail wins over logo, mirroring the wallet builder."""
        organization.logo.save("logo.png", ContentFile(b"original-logo-bytes"), save=True)
        organization.logo_thumbnail.save("logo_thumb.png", ContentFile(png_bytes), save=True)
        url = reverse("api:organization_logo", kwargs={"organization_id": organization.id})

        response = client.get(url)

        assert response.status_code == 200
        assert response.content == png_bytes

    def test_placeholder_when_no_logo(self, client: Client, organization: Organization) -> None:
        """An org without a logo serves a placeholder image, not an error."""
        url = reverse("api:organization_logo", kwargs={"organization_id": organization.id})

        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response.content.startswith(PNG_MAGIC)

    def test_placeholder_when_file_missing_from_storage(
        self, client: Client, organization: Organization, png_bytes: bytes
    ) -> None:
        """A DB-referenced file deleted from storage degrades to the placeholder."""
        organization.logo.save("logo.png", ContentFile(png_bytes), save=True)
        organization.logo.storage.delete(organization.logo.name)
        url = reverse("api:organization_logo", kwargs={"organization_id": organization.id})

        response = client.get(url)

        assert response.status_code == 200
        assert response.content.startswith(PNG_MAGIC)

    def test_private_org_logo_still_served_anonymously(
        self, client: Client, organization: Organization, png_bytes: bytes
    ) -> None:
        """Visibility filtering is bypassed: Google's fetcher is anonymous."""
        organization.visibility = Organization.Visibility.PRIVATE
        organization.save(update_fields=["visibility"])
        organization.logo.save("logo.png", ContentFile(png_bytes), save=True)
        url = reverse("api:organization_logo", kwargs={"organization_id": organization.id})

        response = client.get(url)

        assert response.status_code == 200
        assert response.content == png_bytes

    def test_unknown_org_404s(self, client: Client) -> None:
        url = reverse("api:organization_logo", kwargs={"organization_id": "00000000-0000-0000-0000-000000000000"})

        response = client.get(url)

        assert response.status_code == 404


class TestEventCoverArt:
    def test_serves_cover_art_bytes(self, client: Client, event: Event, png_bytes: bytes) -> None:
        event.cover_art.save("cover.png", ContentFile(png_bytes), save=True)
        url = reverse("api:event_cover_art", kwargs={"event_id": event.id})

        response = client.get(url)

        assert response.status_code == 200
        assert response["Content-Type"] == "image/png"
        assert response.content == png_bytes
        assert "public" in response["Cache-Control"]

    def test_placeholder_when_no_cover_art(self, client: Client, event: Event) -> None:
        url = reverse("api:event_cover_art", kwargs={"event_id": event.id})

        response = client.get(url)

        assert response.status_code == 200
        assert response.content.startswith(PNG_MAGIC)

    def test_unknown_event_404s(self, client: Client) -> None:
        url = reverse("api:event_cover_art", kwargs={"event_id": "00000000-0000-0000-0000-000000000000"})

        response = client.get(url)

        assert response.status_code == 404
