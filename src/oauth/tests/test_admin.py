"""The Unfold admin over the swapped Application and DOT's credential tables (spec §8, §16).

DOT registers all five models itself, from the five ``*_ADMIN_CLASS`` settings, at admin
autodiscovery time (``oauth2_provider/admin.py``). Those settings are the whole wiring: without
them our classes are dead code and the curated sidebar links 404, so the first test here asserts
the registry actually holds our classes rather than DOT's stock ones (R-01).
"""

import datetime as dt
import hashlib
import json
import typing as t

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db.models.fields.files import FieldFile
from django.test import RequestFactory
from django.test.client import Client
from django.urls import NoReverseMatch, reverse
from oauth2_provider.models import AccessToken, Grant, IDToken, RefreshToken
from pytest_django.fixtures import Settings

from accounts.models import RevelUser
from common.authentication import InvalidBearerToken, ScopedJWTAuth
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

    Three shapes need converting: an unset ``FileField`` is a ``FieldFile`` that raises when
    coerced to a string (so an absent logo is simply left out of the body), a ``JSONField``
    posts as one JSON string rather than as repeated values, and a ``datetime`` is rendered by
    the admin's ``SplitDateTimeField`` as two inputs. Getting that last one wrong is not a
    cosmetic bug: a body the form rejects makes every "the admin could not change X" assertion
    pass for the wrong reason, which is exactly how the R-113 boundary test first came up green.

    Args:
        initial: ``adminform.form.initial`` from a rendered change form.

    Returns:
        The urlencoded body.
    """
    data: dict[str, t.Any] = {}
    for name, value in initial.items():
        if isinstance(value, FieldFile):
            continue
        if isinstance(value, dt.datetime):
            data[f"{name}_0"], data[f"{name}_1"] = value.date().isoformat(), value.time().isoformat()
        elif isinstance(value, list | dict):
            data[name] = json.dumps(value)
        else:
            data[name] = "" if value is None else value
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


#: A bearer no OAuth flow ever issued. Hex-free and dot-free so ``ScopedJWTAuth`` treats it as
#: an app token and offers it to DOT rather than short-circuiting it as a malformed session JWT.
FORGED_BEARER = "forged-bearer"

CREDENTIAL_ADMINS: tuple[tuple[type[t.Any], str], ...] = (
    (AccessToken, "token"),
    (Grant, "code"),
    (IDToken, ""),
    (RefreshToken, "token"),
)


@pytest.mark.parametrize(("model", "secret_field"), CREDENTIAL_ADMINS)
def test_credential_admins_are_view_and_revoke_only(
    rf: RequestFactory, superuser: RevelUser, model: type[t.Any], secret_field: str
) -> None:
    """R-113: no field of a credential row may be editable, above all ``token_checksum``.

    Asserted by construction rather than by enumeration: the form must offer *nothing*, so a
    DOT upgrade that adds a field cannot reopen the boundary. ``token``/``code`` staying
    excluded and add being refused are DOT's own guarantees, re-asserted here because this
    module's subclasses are what would break them.

    This is the only non-vacuous guard available for three of the four admins. Only the access
    admin's change form is actually submittable on a live row, and so only it can be shown
    exploited (``test_admin_cannot_forge_a_bearer_by_typing_a_checksum``). ``RefreshToken``
    declares ``revoked = DateTimeField(null=True)`` with no ``blank=True``
    (``oauth2_provider/models.py:789``), which makes the field **required** in the model form,
    so every body that would blank it — or that omits it for an unrevoked token — is rejected by
    Django before it reaches the database. Verified, not assumed: a POST test there passes
    against a *vulnerable* admin, which is why there isn't one.
    """
    model_admin = admin.site._registry[model]
    request = rf.get("/")
    request.user = superuser
    instance = model()
    assert model_admin.has_add_permission(request) is False
    form = model_admin.get_form(request, obj=instance, change=True)
    assert form.base_fields == {}
    assert "token_checksum" not in form.base_fields
    if secret_field:
        assert secret_field in tuple(model_admin.get_exclude(request, instance) or ())


@pytest.mark.parametrize("model", [AccessToken, RefreshToken])
def test_credential_admins_keep_the_revoke_action(model: type[t.Any]) -> None:
    """Readonly must not mean inert: revocation is the whole point of the surface."""
    assert "revoke_tokens" in (admin.site._registry[model].actions or ())


def test_admin_cannot_forge_a_bearer_by_typing_a_checksum(
    admin_client: Client, user: RevelUser, oauth_app: OAuthApplication
) -> None:
    """R-113, demonstrated rather than asserted: the forged bearer must not authenticate.

    Under ``COMPLIANT_BCP_RFC9700_TOKEN_STORAGE`` the ``token`` column is stored blank, so
    ``token_checksum`` is the *sole* bearer verifier — ``OAuth2Validator._load_access_token``
    filters on it alone — and ``TokenChecksumField.pre_save`` keeps a hand-typed value verbatim
    when there is no raw token to recompute from. An editable checksum is therefore a mint:
    paste ``sha256(secret)``, set ``user``/``scope``/``expires`` on the same page, and call the
    API as that user with the ``token`` column still blank, so the row looks untouched.

    The body is built explicitly rather than from ``form.initial``, which is empty once every
    field is readonly — a POST derived from it would be a no-op and would pass for the wrong
    reason. ``expires`` is far enough out that the admin's ``SplitDateTimeField`` re-reading a
    naive time in ``TIME_ZONE`` cannot expire the row and mask the result: that shift is what
    made the first version of this test come up green against a vulnerable admin.
    """
    make_access_token(user, oauth_app, "org:read", expires_in=86_400)
    row = AccessToken.objects.get()
    expires = row.expires
    body = {
        "token_checksum": hashlib.sha256(FORGED_BEARER.encode()).hexdigest(),
        "user": str(user.pk),
        "application": str(oauth_app.pk),
        "scope": "org:read",
        "expires_0": expires.date().isoformat(),
        "expires_1": expires.time().isoformat(),
        "resource": "[]",
        "_save": "Save",
    }
    url = reverse("admin:oauth2_provider_accesstoken_change", args=[row.pk])
    assert admin_client.post(url, body).status_code == 302

    row.refresh_from_db()
    assert row.token_checksum != body["token_checksum"]
    request = RequestFactory().get("/api/x", HTTP_AUTHORIZATION=f"Bearer {FORGED_BEARER}")
    with pytest.raises(InvalidBearerToken):
        ScopedJWTAuth()(request)


def test_admin_scope_shrink_revokes_what_held_the_removed_scope(
    admin_client: Client, user: RevelUser, oauth_app: OAuthApplication
) -> None:
    """R-115: the admin and ``app_service.update_app`` must agree on what withdrawing a scope means.

    ``common.authentication`` states the invariant the other way round — a principal reflects
    what was granted at issue time, not the app's current ``allowed_scopes``, *because*
    shrinking those revokes the live tokens. An admin edit that skipped the revoke would leave
    tokens carrying authority the app no longer has.
    """
    make_access_token(user, oauth_app, "org:read")
    make_access_token(user, oauth_app, "me:read")
    url = reverse("admin:oauth_oauthapplication_change", args=[oauth_app.pk])
    form = admin_client.get(url).context["adminform"].form.initial
    form.update({"allowed_scopes": [s for s in oauth_app.allowed_scopes if s != "org:read"], "_save": "Save"})
    assert admin_client.post(url, _as_post(form)).status_code == 302

    oauth_app.refresh_from_db()
    assert "org:read" not in oauth_app.allowed_scopes
    scopes = set(AccessToken.objects.filter(application=oauth_app).values_list("scope", flat=True))
    assert scopes == {"me:read"}


def test_application_admin_readonly_fields() -> None:
    """The generated columns and the security boundary stay out of the editable set."""
    readonly = admin.site._registry[OAuthApplication].readonly_fields
    assert {"registration_source", "cimd_expires_at", "logo_thumbnail", "client_id", "client_secret"} <= set(readonly)


def test_masked_token_identifies_a_row(user: RevelUser, oauth_app: OAuthApplication) -> None:
    """#7: with RFC 9700 storage ``token`` is blank, so DOT's masked column is always empty.

    An always-empty column cannot correlate a row with anything, which is the only reason to
    show a masked credential at all — so a checksum prefix is shown instead. The checksum is a
    SHA-256 of the bearer, so a prefix of it is not a credential and cannot be reversed into one.
    """
    make_access_token(user, oauth_app, "org:read")
    row = AccessToken.objects.get()
    assert row.token == ""  # the premise: DOT's mask_credential("") returns ""
    model_admin = t.cast(oauth_admin.OAuthAccessTokenAdmin, admin.site._registry[AccessToken])
    rendered = model_admin.masked_token(row)
    assert rendered and rendered in row.token_checksum
