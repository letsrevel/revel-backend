"""The Unfold admin over the swapped Application and DOT's credential tables (spec §8, §16).

DOT registers all five models itself, from the five ``*_ADMIN_CLASS`` settings, at admin
autodiscovery time (``oauth2_provider/admin.py``). Those settings are the whole wiring: without
them our classes are dead code and the curated sidebar links 404, so the first test here asserts
the registry actually holds our classes rather than DOT's stock ones (R-01).
"""

import json
import typing as t

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models.fields.files import FieldFile
from django.test.client import Client
from django.urls import NoReverseMatch, reverse
from oauth2_provider.models import AccessToken, Grant, IDToken, RefreshToken
from pytest_django.fixtures import Settings

from accounts.models import RevelUser
from oauth import admin as oauth_admin
from oauth.models import OAuthApplication
from oauth.tests.test_auth_class import make_access_token

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_manifest_storage(settings: Settings) -> None:
    """Rendering a whole admin page needs a staticfiles manifest whitenoise's storage lacks here.

    Same swap as ``accounts/tests/test_admin_referral_application.py`` and
    ``events/tests/test_admin/test_organization_admin_filters.py``.
    """
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def admin_client(client: Client, superuser: RevelUser) -> Client:
    """A session-authenticated superuser client."""
    client.force_login(superuser)
    return client


def _as_post(initial: dict[str, t.Any]) -> dict[str, t.Any]:
    """Turn an admin form's ``initial`` into a POST body the change view accepts.

    Two shapes need converting: an unset ``FileField`` is a ``FieldFile`` that raises when
    coerced to a string (so an absent logo is simply left out of the body), and a ``JSONField``
    posts as one JSON string rather than as repeated values.

    Args:
        initial: ``adminform.form.initial`` from a rendered change form.

    Returns:
        The multipart/urlencoded body.
    """
    data: dict[str, t.Any] = {}
    for name, value in initial.items():
        if isinstance(value, FieldFile):
            continue
        data[name] = json.dumps(value) if isinstance(value, list | dict) else ("" if value is None else value)
    return data


#: Every model DOT registers, paired with the admin class this app is expected to supply.
REGISTERED: tuple[tuple[type[t.Any], type[t.Any]], ...] = (
    (OAuthApplication, oauth_admin.OAuthApplicationAdmin),
    (AccessToken, oauth_admin.OAuthAccessTokenAdmin),
    (Grant, oauth_admin.OAuthGrantAdmin),
    (IDToken, oauth_admin.OAuthIDTokenAdmin),
    (RefreshToken, oauth_admin.OAuthRefreshTokenAdmin),
)


@pytest.mark.parametrize(("model", "admin_class"), REGISTERED)
def test_admin_settings_resolve_to_our_classes(model: type[t.Any], admin_class: type[t.Any]) -> None:
    """R-01: the five ``*_ADMIN_CLASS`` keys must point at ``oauth.admin``, not DOT's defaults."""
    assert admin.site.is_registered(model)
    assert isinstance(admin.site._registry[model], admin_class)


@pytest.mark.parametrize(
    "viewname",
    [
        "admin:oauth_oauthapplication_changelist",
        "admin:oauth2_provider_accesstoken_changelist",
        "admin:oauth2_provider_grant_changelist",
        "admin:oauth2_provider_idtoken_changelist",
        "admin:oauth2_provider_refreshtoken_changelist",
    ],
)
def test_changelists_reverse_and_render(admin_client: Client, viewname: str) -> None:
    """The curated sidebar reverses these at settings-import time; they must also render."""
    response = admin_client.get(reverse(viewname))
    assert response.status_code == 200


def test_sidebar_links_reverse(settings: Settings) -> None:
    """``show_all_applications`` is False, so a missing sidebar entry means an invisible admin."""
    groups = settings.UNFOLD["SIDEBAR"]["navigation"]
    titles = {str(item["title"]) for group in groups for item in group["items"]}
    assert {"OAuth Apps", "OAuth Access Tokens", "OAuth Refresh Tokens"} <= titles
    # ``reverse_lazy`` defers resolution, so force every link to prove none of them is broken.
    for group in groups:
        for item in group["items"]:
            try:
                str(item["link"])
            except NoReverseMatch as exc:  # pragma: no cover - a broken link fails the assert
                pytest.fail(f"sidebar link does not reverse: {item['title']} ({exc})")


def test_admin_deactivation_revokes_tokens(admin_client: Client, oauth_app: OAuthApplication) -> None:
    """Flipping ``is_active`` off in the admin must reach DOT's tables, not just the flag."""
    make_access_token(oauth_app.user, oauth_app, "org:read")
    url = reverse("admin:oauth_oauthapplication_change", args=[oauth_app.pk])
    form = admin_client.get(url).context["adminform"].form.initial
    form.update({"is_active": False, "_save": "Save"})
    # 302 rather than "200 or 302": a re-rendered form means validation failed and nothing saved.
    assert admin_client.post(url, _as_post(form)).status_code == 302
    oauth_app.refresh_from_db()
    assert oauth_app.is_active is False
    assert not AccessToken.objects.filter(application=oauth_app).exists()


def test_admin_keeps_tokens_when_other_fields_change(admin_client: Client, oauth_app: OAuthApplication) -> None:
    """A rename is not a revocation; only an ``is_active`` flip revokes."""
    make_access_token(oauth_app.user, oauth_app, "org:read")
    url = reverse("admin:oauth_oauthapplication_change", args=[oauth_app.pk])
    form = admin_client.get(url).context["adminform"].form.initial
    form.update({"name": "Renamed App", "_save": "Save"})
    assert admin_client.post(url, _as_post(form)).status_code == 302
    oauth_app.refresh_from_db()
    assert oauth_app.name == "Renamed App"  # the save really happened, so the token really survived
    assert AccessToken.objects.filter(application=oauth_app).exists()


def test_registration_source_stays_readonly(admin_client: Client, oauth_app: OAuthApplication) -> None:
    """DOT treats ``registration_source`` as a security boundary: the RFC 7592 management
    endpoint only operates on DCR apps, so the admin must not be able to flip a manual app into
    that code path."""
    url = reverse("admin:oauth_oauthapplication_change", args=[oauth_app.pk])
    form = admin_client.get(url).context["adminform"].form.initial
    form.update({"registration_source": OAuthApplication.RegistrationSource.DCR, "_save": "Save"})
    assert admin_client.post(url, _as_post(form)).status_code == 302
    oauth_app.refresh_from_db()
    assert oauth_app.registration_source == OAuthApplication.RegistrationSource.MANUAL


def test_add_form_explains_the_missing_signing_key(settings: Settings, admin_client: Client) -> None:
    """R-98/R-36: with no signing key there is no valid ``algorithm``, and that is by design.

    DOT's ``clean()`` rejects RS256 without ``OIDC_RSA_PRIVATE_KEY`` and its ``NO_ALGORITHM`` is
    ``""``, which our ``blank=False`` field refuses — so the app simply cannot be created. The
    admin must say why rather than 500, which is what the help text is for.
    """
    settings.OIDC_SIGNING_KEY_PATH = ""
    settings.OAUTH2_PROVIDER = {**settings.OAUTH2_PROVIDER, "OIDC_RSA_PRIVATE_KEY": ""}
    response = admin_client.get(reverse("admin:oauth_oauthapplication_add"))
    assert response.status_code == 200
    assert "no signing key" in response.context["adminform"].form.fields["algorithm"].help_text.lower()


def test_rs256_without_a_signing_key_is_a_field_error(settings: Settings, user: RevelUser) -> None:
    """The same rule at the model layer: a comprehensible field error, not an exception elsewhere."""
    settings.OAUTH2_PROVIDER = {**settings.OAUTH2_PROVIDER, "OIDC_RSA_PRIVATE_KEY": ""}
    app = OAuthApplication(
        user=user,
        name="No Key",
        client_type=OAuthApplication.CLIENT_CONFIDENTIAL,
        redirect_uris="https://app.example/cb",
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
    )
    with pytest.raises(ValidationError) as excinfo:
        app.full_clean()
    assert "algorithm" in excinfo.value.message_dict


def test_version_advertises_provider(client: Client) -> None:
    assert client.get("/api/version").json()["features"]["oauth_provider"] is True


def test_version_hides_disabled_provider(settings: Settings, client: Client) -> None:
    settings.OIDC_SIGNING_KEY_PATH = ""
    assert client.get("/api/version").json()["features"]["oauth_provider"] is False
