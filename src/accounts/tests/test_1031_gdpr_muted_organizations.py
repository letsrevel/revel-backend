"""GDPR export includes the per-organization announcement mutes (#1031)."""

import json
import zipfile
from io import BytesIO

import pytest

from accounts.models import RevelUser
from accounts.service import gdpr
from events.models import Organization

pytestmark = pytest.mark.django_db


def _export(user: RevelUser) -> dict[str, object]:
    export = gdpr.generate_user_data_export(user)
    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        with zip_file.open("revel_user_data.json") as json_file:
            data: dict[str, object] = json.load(json_file)
    return data


def test_export_lists_muted_organizations_as_summaries(user: RevelUser) -> None:
    other = RevelUser.objects.create_user(username="mute-gdpr-owner", email="mute-gdpr-owner@example.com")
    org = Organization.objects.create(name="Muted Org", slug="muted-gdpr-org", owner=other)
    user.notification_preferences.muted_organizations.add(org)
    user.refresh_from_db()

    prefs = _export(user)["notification_preferences"]

    assert isinstance(prefs, dict)
    assert prefs["muted_organizations"] == [{"id": str(org.id), "name": "Muted Org", "slug": "muted-gdpr-org"}]
    assert "enabled_channels" in prefs


def test_export_without_preferences_row(user: RevelUser) -> None:
    user.notification_preferences.delete()
    user = RevelUser.objects.get(pk=user.pk)

    assert _export(user)["notification_preferences"] is None
