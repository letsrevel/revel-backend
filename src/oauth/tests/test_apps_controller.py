"""Tests for the developer portal at ``/api/oauth/apps`` (spec §8.3).

This is the surface a developer uses to register a client other people will then be asked to
trust, so the assertions here are as much about what it refuses (``skip_authorization``, another
owner's app, an unverified email, a stored secret) as about what it creates.
"""

import typing as t

import pytest
from django.contrib.auth.hashers import check_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.client import Client
from oauth2_provider.models import AccessToken, RefreshToken

from accounts.models import RevelUser
from common.models import FileUploadAudit
from oauth.models import OAuthApplication
from oauth.tests.test_auth_class import make_access_token
from oauth.tests.test_token_service import make_token_pair

pytestmark = pytest.mark.django_db

CREATE: dict[str, t.Any] = {
    "name": "My App",
    "description": "d",
    "client_type": "confidential",
    "redirect_uris": ["https://x.example/cb"],
    "allowed_scopes": ["openid", "org:read"],
    "homepage_url": "https://x.example",
    "privacy_policy_url": "https://x.example/privacy",
}


def create_app(session_client: Client, **overrides: t.Any) -> dict[str, t.Any]:
    """POST the create payload and return the response body, asserting it was created."""
    response = session_client.post("/api/oauth/apps/", data={**CREATE, **overrides}, content_type="application/json")
    assert response.status_code == 201, response.content
    return t.cast(dict[str, t.Any], response.json())


def test_create_returns_secret_once(session_client: Client) -> None:
    data = create_app(session_client)
    assert data["client_id"] and data["client_secret"]
    assert "client_secret" not in session_client.get(f"/api/oauth/apps/{data['id']}").json()


def test_create_never_returns_the_stored_secret(session_client: Client) -> None:
    """The column holds a hash (R-33), so the plaintext must come from the generator, not the row."""
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    assert data["client_secret"] != app.client_secret
    assert check_password(data["client_secret"], app.client_secret)


def test_public_app_has_no_secret(session_client: Client) -> None:
    data = create_app(session_client, client_type="public")
    assert data["client_secret"] is None


def test_create_ignores_skip_authorization(session_client: Client) -> None:
    """R-78: the consent tree trusts this flag, so a developer must never be able to set it."""
    data = create_app(session_client, skip_authorization=True)
    assert "skip_authorization" not in data
    assert OAuthApplication.objects.get(pk=data["id"]).skip_authorization is False


def test_update_ignores_skip_authorization(session_client: Client) -> None:
    data = create_app(session_client)
    response = session_client.patch(
        f"/api/oauth/apps/{data['id']}",
        data={"skip_authorization": True},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert OAuthApplication.objects.get(pk=data["id"]).skip_authorization is False


def test_create_is_always_manually_registered(session_client: Client) -> None:
    data = create_app(session_client, registration_source="dcr")
    assert data["registration_source"] == OAuthApplication.RegistrationSource.MANUAL


def test_unknown_scope_rejected(session_client: Client) -> None:
    response = session_client.post(
        "/api/oauth/apps/",
        data={**CREATE, "allowed_scopes": ["openid", "not:a:scope"]},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content


def test_plain_http_redirect_uri_rejected(session_client: Client) -> None:
    response = session_client.post(
        "/api/oauth/apps/",
        data={**CREATE, "redirect_uris": ["http://evil.example/cb"]},
        content_type="application/json",
    )
    assert response.status_code == 400, response.content


def test_cap(settings: t.Any, session_client: Client) -> None:
    settings.OAUTH_MAX_APPS_PER_USER = 1
    assert session_client.post("/api/oauth/apps/", data=CREATE, content_type="application/json").status_code == 201
    assert session_client.post("/api/oauth/apps/", data=CREATE, content_type="application/json").status_code == 409


def test_unverified_email_forbidden(user: RevelUser, session_client: Client) -> None:
    """R-73: verification gates *publishing* an app (spec §8.3)."""
    user.email_verified = False
    user.save(update_fields=["email_verified"])
    assert session_client.get("/api/oauth/apps/").status_code == 403


def test_list_only_mine(session_client: Client, revel_user_factory: t.Any) -> None:
    other = revel_user_factory()
    OAuthApplication.objects.create(
        user=other,
        name="Theirs",
        client_type="confidential",
        redirect_uris="https://y.example/cb",
        allowed_scopes=[],
    )
    create_app(session_client)
    names = [a["name"] for a in session_client.get("/api/oauth/apps/").json()]
    assert names == ["My App"]


def test_another_users_app_is_not_reachable(session_client: Client, revel_user_factory: t.Any) -> None:
    theirs = OAuthApplication.objects.create(
        user=revel_user_factory(),
        name="Theirs",
        client_type="confidential",
        redirect_uris="https://y.example/cb",
        allowed_scopes=[],
    )
    assert session_client.get(f"/api/oauth/apps/{theirs.pk}").status_code == 404
    assert (
        session_client.patch(
            f"/api/oauth/apps/{theirs.pk}", data={"name": "Mine now"}, content_type="application/json"
        ).status_code
        == 404
    )
    assert session_client.delete(f"/api/oauth/apps/{theirs.pk}").status_code == 404
    assert session_client.post(f"/api/oauth/apps/{theirs.pk}/rotate-secret").status_code == 404
    theirs.refresh_from_db()
    assert theirs.name == "Theirs"


def test_update_fields(session_client: Client) -> None:
    data = create_app(session_client)
    response = session_client.patch(
        f"/api/oauth/apps/{data['id']}",
        data={"name": "Renamed", "redirect_uris": ["https://x.example/cb", "https://x.example/cb2"]},
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["redirect_uris"] == ["https://x.example/cb", "https://x.example/cb2"]


def test_empty_update_is_a_no_op(session_client: Client) -> None:
    data = create_app(session_client)
    response = session_client.patch(f"/api/oauth/apps/{data['id']}", data={}, content_type="application/json")
    assert response.status_code == 200, response.content
    assert response.json()["name"] == "My App"


def test_scope_shrink_revokes_tokens(session_client: Client, user: RevelUser) -> None:
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    _access, refresh = make_token_pair(user, app, "openid org:read")
    response = session_client.patch(
        f"/api/oauth/apps/{app.pk}", data={"allowed_scopes": ["openid"]}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    app.refresh_from_db()
    assert app.allowed_scopes == ["openid"]
    assert not AccessToken.objects.filter(application=app).exists()
    refresh.refresh_from_db()
    assert refresh.revoked is not None


def test_scope_shrink_spares_tokens_without_the_removed_scope(session_client: Client, user: RevelUser) -> None:
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    make_access_token(user, app, "openid")
    response = session_client.patch(
        f"/api/oauth/apps/{app.pk}", data={"allowed_scopes": ["openid"]}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert AccessToken.objects.filter(application=app).count() == 1


def test_scope_shrink_to_nothing_is_not_re_seeded(session_client: Client) -> None:
    data = create_app(session_client)
    response = session_client.patch(
        f"/api/oauth/apps/{data['id']}", data={"allowed_scopes": []}, content_type="application/json"
    )
    assert response.status_code == 200, response.content
    assert OAuthApplication.objects.get(pk=data["id"]).allowed_scopes == []


def test_rotate_secret(session_client: Client) -> None:
    data = create_app(session_client)
    response = session_client.post(f"/api/oauth/apps/{data['id']}/rotate-secret")
    assert response.status_code == 200, response.content
    new = response.json()["client_secret"]
    assert new and new != data["client_secret"]
    assert check_password(new, OAuthApplication.objects.get(pk=data["id"]).client_secret)


def test_rotate_secret_refused_for_a_public_client(session_client: Client) -> None:
    data = create_app(session_client, client_type="public")
    assert session_client.post(f"/api/oauth/apps/{data['id']}/rotate-secret").status_code == 400


def test_deactivate_revokes(session_client: Client, user: RevelUser) -> None:
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    _access, refresh = make_token_pair(user, app, "openid")
    response = session_client.post(f"/api/oauth/apps/{app.pk}/deactivate")
    assert response.status_code == 200, response.content
    app.refresh_from_db()
    assert app.is_active is False
    assert not AccessToken.objects.filter(application=app).exists()
    refresh.refresh_from_db()
    assert refresh.revoked is not None


def test_activate(session_client: Client) -> None:
    data = create_app(session_client)
    session_client.post(f"/api/oauth/apps/{data['id']}/deactivate")
    response = session_client.post(f"/api/oauth/apps/{data['id']}/activate")
    assert response.status_code == 200, response.content
    assert response.json()["is_active"] is True


def test_delete_revokes_and_removes(session_client: Client, user: RevelUser) -> None:
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    make_token_pair(user, app, "openid")
    assert session_client.delete(f"/api/oauth/apps/{app.pk}").status_code == 204
    assert not OAuthApplication.objects.filter(pk=app.pk).exists()
    assert not AccessToken.objects.filter(application_id=app.pk).exists()
    assert not RefreshToken.objects.filter(application_id=app.pk).exists()


def test_connections_count_but_never_users(session_client: Client, user: RevelUser, revel_user_factory: t.Any) -> None:
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    make_access_token(user, app, "openid")
    make_access_token(revel_user_factory(), app, "openid")
    detail = session_client.get(f"/api/oauth/apps/{app.pk}").json()
    assert detail["connections_count"] == 2
    assert "users" not in detail and "connections" not in detail
    assert not {"user", "user_id", "email"} & set(detail)


def test_connections_count_ignores_expired_tokens(session_client: Client, user: RevelUser) -> None:
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    make_access_token(user, app, "openid", expires_in=-1)
    assert session_client.get(f"/api/oauth/apps/{app.pk}").json()["connections_count"] == 0


def test_logo_upload_generates_a_thumbnail(
    session_client: Client, png_bytes: bytes, django_capture_on_commit_callbacks: t.Any
) -> None:
    """R-35: ``safe_save_uploaded_file`` is the only dispatch site for ``THUMBNAIL_CONFIGS``.

    A bare ``save()`` would store the logo and leave ``logo_thumbnail`` empty forever, so the
    thumbnail is the assertion: it exists only if the upload went through the shared service —
    which also means it went through the malware scan and the field validators. Reaching this at
    all needed the UUID primary key (R-82): the service audits every upload in
    ``FileUploadAudit``, whose ``instance_pk`` is a ``UUIDField``.
    """
    data = create_app(session_client)
    logo = SimpleUploadedFile(name="logo.png", content=png_bytes, content_type="image/png")
    with django_capture_on_commit_callbacks(execute=True):
        response = session_client.post(f"/api/oauth/apps/{data['id']}/logo", data={"logo": logo})
    assert response.status_code == 200, response.content
    app = OAuthApplication.objects.get(pk=data["id"])
    assert app.logo
    assert app.logo_thumbnail
    audit = FileUploadAudit.objects.get(app="oauth", model="oauthapplication", field="logo")
    assert audit.instance_pk == app.pk


def test_logo_upload_rejects_a_non_image(session_client: Client) -> None:
    data = create_app(session_client)
    junk = SimpleUploadedFile(name="logo.png", content=b"not an image", content_type="image/png")
    assert session_client.post(f"/api/oauth/apps/{data['id']}/logo", data={"logo": junk}).status_code == 400


def test_logo_url_is_absolute(settings: t.Any, session_client: Client) -> None:
    """R-72: never the root-relative ``logo_thumbnail.url`` — the consent screen is another origin."""
    data = create_app(session_client)
    app = OAuthApplication.objects.get(pk=data["id"])
    app.logo_thumbnail = "oauth-logos/logo.png"
    app.save(update_fields=["logo_thumbnail", "updated"])
    logo_url = session_client.get(f"/api/oauth/apps/{app.pk}").json()["logo_url"]
    assert logo_url == f"{settings.OAUTH_ISSUER}{settings.MEDIA_URL}oauth-logos/logo.png"


def test_logo_url_is_none_without_a_logo(session_client: Client) -> None:
    assert create_app(session_client)["logo_url"] is None
