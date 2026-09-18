"""Unfold admin for the swapped Application and DOT's token models (spec §8, §16).

Nothing here is registered with ``@admin.register``. DOT's own ``oauth2_provider/admin.py``
registers all five models from the five ``*_ADMIN_CLASS`` settings in
``revel/settings/oauth.py``, which is why those settings had to wait for this module to exist
(R-01) and why ``oauth/tests/test_admin.py`` asserts the registry really holds these classes.

Unfold comes first in every MRO so its templates and widgets win over DOT's plain
``ModelAdmin`` (repo precedent: ``events/admin/announcement.py``).
"""

import typing as t

from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from oauth2_provider.admin import AccessTokenAdmin, ApplicationAdmin, GrantAdmin, IDTokenAdmin, RefreshTokenAdmin
from oauth2_provider.settings import oauth2_settings
from unfold.admin import ModelAdmin

from oauth.models import OAuthApplication
from oauth.service import token_service

#: Shown on ``algorithm`` when no signing key is configured. With the provider off there is no
#: valid value for the field at all — DOT's ``clean()`` refuses RS256 without
#: ``OIDC_RSA_PRIVATE_KEY`` and its ``NO_ALGORITHM`` is ``""``, which our ``blank=False`` field
#: refuses in turn. That is the credential-presence feature flag working as designed (ADR-0008,
#: R-36/R-98), so the help text explains it instead of the operator rediscovering it from a
#: validation error (#986).
_NO_SIGNING_KEY_HELP = _(
    "The OAuth provider has no signing key configured, so no application can be saved: "
    "RS256 requires OIDC_SIGNING_KEY_PATH to point at an RSA private key PEM, and the "
    '"no algorithm" option is not accepted for this field. Configure the key first.'
)


class OAuthApplicationAdmin(ModelAdmin, ApplicationAdmin):  # type: ignore[misc]
    """The registered clients, with a revocation hook on deactivation."""

    list_display = (
        "name",
        "client_id",
        "user",
        "client_type",
        "registration_source",
        "verified",
        "is_active",
        "last_used_at",
    )
    list_filter = ("registration_source", "client_type", "verified", "is_active")
    search_fields = ("name", "client_id", "user__email")
    # ``registration_source`` and ``cimd_expires_at`` are DOT's own readonly set and are kept
    # here deliberately: ``registration_source`` is a security boundary (the RFC 7592 management
    # endpoint only operates on DCR-registered apps), so overriding the tuple without them would
    # silently make a manual app promotable into that code path. ``client_secret`` is a stored
    # hash, not a usable credential, and is rotated through the API's rotate-secret endpoint.
    readonly_fields = (
        "registration_source",
        "cimd_expires_at",
        "client_id",
        "client_secret",
        "last_used_at",
        "created",
        "updated",
    )

    def get_form(self, request: HttpRequest, obj: t.Any = None, change: bool = False, **kwargs: t.Any) -> t.Any:
        """Explain an unconfigured provider on the field that will reject the save.

        Args:
            request: The admin request.
            obj: The instance being edited, or None on the add form.
            change: Whether this is a change form.
            **kwargs: Passed through to ``modelform_factory``.

        Returns:
            The model form class.
        """
        if not oauth2_settings.OIDC_RSA_PRIVATE_KEY:
            kwargs["help_texts"] = {"algorithm": _NO_SIGNING_KEY_HELP, **kwargs.get("help_texts", {})}
        return super().get_form(request, obj, change=change, **kwargs)

    def save_model(self, request: HttpRequest, obj: OAuthApplication, form: t.Any, change: bool) -> None:
        """Save, then revoke everything the app holds if it was just deactivated.

        ``is_usable()`` already refuses a deactivated app at request time, so this is not what
        locks the client out — it is what stops a re-activation resurrecting live credentials,
        and what stops ``authorize_service.has_prior_grant`` treating the surviving refresh
        tokens as a prior grant and silently re-approving on the next authorize.

        Args:
            request: The admin request.
            obj: The application being saved.
            form: The bound admin form.
            change: False on an add.
        """
        super().save_model(request, obj, form, change)
        if change and "is_active" in form.changed_data and not obj.is_active:
            token_service.revoke_app_tokens(obj)


class OAuthAccessTokenAdmin(ModelAdmin, AccessTokenAdmin):  # type: ignore[misc]
    """DOT's access-token admin under Unfold; DOT masks the token and forbids add/delete."""


class OAuthGrantAdmin(ModelAdmin, GrantAdmin):  # type: ignore[misc]
    """DOT's grant admin under Unfold."""


class OAuthIDTokenAdmin(ModelAdmin, IDTokenAdmin):  # type: ignore[misc]
    """DOT's ID-token admin under Unfold."""


class OAuthRefreshTokenAdmin(ModelAdmin, RefreshTokenAdmin):  # type: ignore[misc]
    """DOT's refresh-token admin under Unfold; its revoke action keeps the reuse tombstone."""
