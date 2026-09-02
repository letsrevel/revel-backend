"""Tests for the env-backed OIDC provider configuration parser."""

import typing as t

import pytest
from django.core.exceptions import ImproperlyConfigured

from revel.oidc_config import OIDCProviderConfig, load_oidc_providers


def _env(values: dict[str, str]) -> t.Callable[..., t.Any]:
    """Mimic ``decouple.config``: ``get(name, default=...)``."""

    def get(name: str, default: t.Any = None, **_: t.Any) -> t.Any:
        return values.get(name, default)

    return get


def test_empty_list_means_no_providers() -> None:
    assert load_oidc_providers(_env({"OIDC_PROVIDERS": ""}), debug=False) == ()
    assert load_oidc_providers(_env({}), debug=False) == ()


def test_parses_one_provider_with_defaults() -> None:
    providers = load_oidc_providers(
        _env(
            {
                "OIDC_PROVIDERS": "google",
                "OIDC_GOOGLE_ISSUER": "https://accounts.google.com",
                "OIDC_GOOGLE_CLIENT_ID": "cid",
                "OIDC_GOOGLE_CLIENT_SECRET": "sec",
            }
        ),
        debug=False,
    )
    assert providers == (
        OIDCProviderConfig(
            key="google",
            name="Google",
            issuer="https://accounts.google.com",
            client_id="cid",
            client_secret="sec",
            scopes="openid email profile",
        ),
    )


def test_parses_multiple_providers_with_overrides() -> None:
    providers = load_oidc_providers(
        _env(
            {
                "OIDC_PROVIDERS": "google, keycloak",
                "OIDC_GOOGLE_ISSUER": "https://accounts.google.com",
                "OIDC_GOOGLE_CLIENT_ID": "cid",
                "OIDC_GOOGLE_CLIENT_SECRET": "sec",
                "OIDC_KEYCLOAK_NAME": "Uni Login",
                "OIDC_KEYCLOAK_ISSUER": "https://kc.example.org/realms/uni",
                "OIDC_KEYCLOAK_CLIENT_ID": "revel",
                "OIDC_KEYCLOAK_CLIENT_SECRET": "s",
                "OIDC_KEYCLOAK_SCOPES": "openid email",
            }
        ),
        debug=False,
    )
    assert [p.key for p in providers] == ["google", "keycloak"]
    assert providers[1].name == "Uni Login"
    assert providers[1].scopes == "openid email"


@pytest.mark.parametrize("missing", ["OIDC_GOOGLE_ISSUER", "OIDC_GOOGLE_CLIENT_ID", "OIDC_GOOGLE_CLIENT_SECRET"])
def test_missing_required_var_raises(missing: str) -> None:
    values = {
        "OIDC_PROVIDERS": "google",
        "OIDC_GOOGLE_ISSUER": "https://accounts.google.com",
        "OIDC_GOOGLE_CLIENT_ID": "cid",
        "OIDC_GOOGLE_CLIENT_SECRET": "sec",
    }
    del values[missing]
    with pytest.raises(ImproperlyConfigured, match=missing):
        load_oidc_providers(_env(values), debug=False)


def test_http_issuer_rejected_unless_debug() -> None:
    values = {
        "OIDC_PROVIDERS": "local",
        "OIDC_LOCAL_ISSUER": "http://localhost:8080/realms/dev",
        "OIDC_LOCAL_CLIENT_ID": "cid",
        "OIDC_LOCAL_CLIENT_SECRET": "sec",
    }
    with pytest.raises(ImproperlyConfigured, match="https"):
        load_oidc_providers(_env(values), debug=False)
    assert load_oidc_providers(_env(values), debug=True)[0].issuer == "http://localhost:8080/realms/dev"


def test_issuer_trailing_slash_is_stripped() -> None:
    providers = load_oidc_providers(
        _env(
            {
                "OIDC_PROVIDERS": "google",
                "OIDC_GOOGLE_ISSUER": "https://accounts.google.com/",
                "OIDC_GOOGLE_CLIENT_ID": "cid",
                "OIDC_GOOGLE_CLIENT_SECRET": "sec",
            }
        ),
        debug=False,
    )
    assert providers[0].issuer == "https://accounts.google.com"


@pytest.mark.parametrize("bad_key", ["Google", "my provider", "a/b"])
def test_invalid_key_rejected(bad_key: str) -> None:
    with pytest.raises(ImproperlyConfigured, match="key"):
        load_oidc_providers(_env({"OIDC_PROVIDERS": bad_key}), debug=False)
