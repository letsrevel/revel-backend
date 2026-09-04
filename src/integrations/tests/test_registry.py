"""Provider registry: enablement follows settings; unknown keys 404."""

import pytest

from integrations import registry
from integrations.exceptions import IntegrationError
from integrations.providers.base import ListingProvider
from integrations.schema import IntegrationErrorCode
from integrations.tests.fake_provider import FakeProvider


def test_fake_provider_satisfies_protocol() -> None:
    assert isinstance(FakeProvider(), ListingProvider)


def test_eventbrite_disabled_without_credentials(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "INTEGRATIONS_EVENTBRITE_CLIENT_ID", "")
    monkeypatch.setattr(settings, "INTEGRATIONS_EVENTBRITE_CLIENT_SECRET", "")
    registry.populate()
    assert [p.key for p in registry.enabled_providers()] == []


def test_eventbrite_enabled_with_credentials(settings: object, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "INTEGRATIONS_EVENTBRITE_CLIENT_ID", "id")
    monkeypatch.setattr(settings, "INTEGRATIONS_EVENTBRITE_CLIENT_SECRET", "secret")
    registry.populate()
    assert [p.key for p in registry.enabled_providers()] == ["eventbrite"]
    assert registry.get_provider("eventbrite").display_name == "Eventbrite"


def test_get_unknown_provider_raises_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "PROVIDERS", {})
    with pytest.raises(IntegrationError) as exc:
        registry.get_provider("nope")
    assert exc.value.code == IntegrationErrorCode.PROVIDER_UNKNOWN
    assert exc.value.status == 404
