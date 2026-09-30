"""GDPR export includes the user's current email suppression status (#1038)."""

import json
import zipfile
from io import BytesIO

import pytest

from accounts.models import RevelUser
from accounts.service import gdpr
from notifications.models import EmailSuppression

pytestmark = pytest.mark.django_db


def _export(user: RevelUser) -> dict[str, object]:
    export = gdpr.generate_user_data_export(user)
    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        with zip_file.open("revel_user_data.json") as json_file:
            data: dict[str, object] = json.load(json_file)
    return data


def test_export_includes_suppression_reason_and_since(user: RevelUser) -> None:
    row = EmailSuppression.objects.create(
        email=user.email,
        reason=EmailSuppression.Reason.BLOCKED,
        source=EmailSuppression.Source.PROVIDER,
        detail="provider internals",
    )

    status = _export(user)["email_suppression"]

    assert isinstance(status, dict)
    assert set(status) == {"reason", "since"}
    assert status["reason"] == "blocked"
    assert str(status["since"]).startswith(row.updated_at.strftime("%Y-%m-%dT%H:%M:%S"))


def test_export_without_suppression_is_null(user: RevelUser) -> None:
    assert _export(user)["email_suppression"] is None
