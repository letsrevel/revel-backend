"""Developer-app lifecycle for the self-service portal (spec §8.3)."""

import typing as t

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from oauth2_provider.generators import generate_client_secret

from accounts.models import RevelUser
from oauth import schema
from oauth.exceptions import AppLimitReachedError
from oauth.models import OAuthApplication
from oauth.service import token_service


def create_app(user: RevelUser, payload: schema.OAuthAppCreatePayload) -> tuple[OAuthApplication, str | None]:
    """Register a manual app owned by ``user``.

    Every field is copied across by name rather than by unpacking the payload, so a field added
    to the schema later cannot silently become a field set on the model — which is the whole
    reason ``skip_authorization`` is absent from the schema in the first place (R-78).

    Args:
        user: The owner-to-be.
        payload: The validated registration request.

    Returns:
        The saved app and, for a confidential client, its plaintext secret — the only time it
        exists in readable form, since the column stores a hash.

    Raises:
        AppLimitReachedError: The user already owns ``OAUTH_MAX_APPS_PER_USER`` apps.
        ValidationError: ``OAuthApplication.clean()`` refused the redirect URIs or the scopes.
    """
    # ponytail: the cap is checked outside a lock, so two concurrent creates can both pass it
    # and leave the user one app over the limit. Upgrading means locking the user's rows (or a
    # DB-level count constraint); it is not done because the ceiling is anti-abuse rather than
    # a security boundary, and the next create is refused either way.
    if OAuthApplication.objects.filter(user=user).count() >= settings.OAUTH_MAX_APPS_PER_USER:
        raise AppLimitReachedError()
    confidential = payload.client_type == OAuthApplication.CLIENT_CONFIDENTIAL
    raw_secret: str = generate_client_secret() if confidential else ""
    app = OAuthApplication(
        user=user,
        registration_source=OAuthApplication.RegistrationSource.MANUAL,
        client_secret=raw_secret,
        name=payload.name,
        description=payload.description,
        client_type=payload.client_type,
        redirect_uris=" ".join(payload.redirect_uris),
        allowed_scopes=payload.allowed_scopes,
        homepage_url=payload.homepage_url,
        privacy_policy_url=payload.privacy_policy_url,
    )
    app.full_clean()
    app.save()
    return app, (raw_secret or None)


@transaction.atomic
def update_app(app: OAuthApplication, data: dict[str, t.Any]) -> OAuthApplication:
    """Apply a partial update; narrowing ``allowed_scopes`` revokes what held a removed scope.

    The revocation is the point of the transaction: an app whose scopes were narrowed must not
    keep handing out tokens that carry the scope its owner just withdrew.
    ``OAuthApplication.save()`` deliberately re-seeds ``allowed_scopes`` on insert only, so
    shrinking to nothing stays nothing instead of inverting into the widest possible grant.

    Args:
        app: The app being edited, already proven to belong to the caller.
        data: ``OAuthAppUpdatePayload.model_dump(exclude_unset=True)`` — the payload is the
            allow-list of what a developer may change.

    Returns:
        The saved app.

    Raises:
        ValidationError: ``OAuthApplication.clean()`` refused the new values.
    """
    fields = dict(data)
    if "redirect_uris" in fields:
        fields["redirect_uris"] = " ".join(fields["redirect_uris"])
    removed = set(app.allowed_scopes) - set(fields.get("allowed_scopes", app.allowed_scopes))
    for field, value in fields.items():
        setattr(app, field, value)
    app.full_clean()
    app.save(update_fields=[*fields, "updated"])
    if removed:
        token_service.revoke_scoped_tokens(app, removed)
    return app


def rotate_secret(app: OAuthApplication) -> str:
    """Issue a new client secret and return its plaintext.

    Existing tokens survive: the secret authenticates the *client* at the token endpoint and is
    not what a user granted, so re-keying is a credential rotation, not a revocation.

    Args:
        app: The confidential app being re-keyed.

    Returns:
        The new plaintext secret; the column keeps only its hash.

    Raises:
        ValidationError: The app is a public client, which has no secret to rotate.
    """
    if app.client_type != OAuthApplication.CLIENT_CONFIDENTIAL:
        raise ValidationError({"client_type": [str(_("A public client has no client secret to rotate."))]})
    # Annotated because DOT's generator is untyped, and mypy --strict refuses to return Any.
    raw: str = generate_client_secret()
    app.client_secret = raw
    # ``updated`` is ``auto_now``, which only fires for fields named in ``update_fields``.
    app.save(update_fields=["client_secret", "updated"])
    return raw


def set_active(app: OAuthApplication, active: bool) -> OAuthApplication:
    """Switch an app on or off, revoking everything it holds when switching it off.

    ``OAuthApplication.is_usable()`` already refuses a deactivated app at every DOT entry point,
    so revoking is belt and braces for the in-flight tokens rather than the gate itself.

    Args:
        app: The app being toggled.
        active: The new state.

    Returns:
        The saved app.
    """
    app.is_active = active
    app.save(update_fields=["is_active", "updated"])
    if not active:
        token_service.revoke_app_tokens(app)
    return app


def delete_app(app: OAuthApplication) -> None:
    """Delete an app, which is the strongest revocation available.

    Every credential table FKs the application with ``on_delete=CASCADE`` (access tokens,
    refresh tokens, ID tokens and pending grants), so there is nothing left to revoke
    afterwards — ``test_delete_revokes_and_removes`` pins that assumption rather than paying for
    a redundant sweep on every delete.

    Args:
        app: The app to delete.
    """
    app.delete()
