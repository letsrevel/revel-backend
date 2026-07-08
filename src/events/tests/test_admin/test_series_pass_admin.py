"""Smoke tests for SeriesPass/SeriesPassTierLink/HeldSeriesPass admin registration."""

import typing as t

import pytest
from django.contrib import admin
from django.test import override_settings
from django.urls import reverse

from events.models import HeldSeriesPass, SeriesPass, SeriesPassTierLink

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("model", [SeriesPass, SeriesPassTierLink, HeldSeriesPass])
def test_series_pass_models_registered_in_admin(model: type[t.Any]) -> None:
    assert admin.site.is_registered(model)


@pytest.mark.parametrize(
    "url_name",
    [
        "admin:events_seriespass_changelist",
        "admin:events_seriespasstierlink_changelist",
        "admin:events_heldseriespass_changelist",
    ],
)
def test_series_pass_admin_changelist_url_resolves(url_name: str) -> None:
    assert reverse(url_name)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
@pytest.mark.parametrize(
    "url_name",
    [
        "admin:events_seriespass_changelist",
        "admin:events_seriespasstierlink_changelist",
        "admin:events_heldseriespass_changelist",
    ],
)
def test_series_pass_admin_changelist_renders(
    admin_client: t.Any,  # pytest-django built-in fixture
    url_name: str,
) -> None:
    resp = admin_client.get(reverse(url_name))
    assert resp.status_code == 200
