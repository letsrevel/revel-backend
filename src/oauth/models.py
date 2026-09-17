"""Swapped django-oauth-toolkit Application model (spec §6.1). Token models stay DOT's."""

import typing as t
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator
from django.db import models
from django.utils.translation import gettext_lazy as _
from oauth2_provider.models import AbstractApplication

from common.fields import ALLOWED_IMAGE_EXTENSIONS, validate_image_file
from common.models import ExifStripMixin

# Compared against ``urlparse(...).hostname``, which strips the brackets from an IPv6
# literal and lowercases it — so "::1", never "[::1]". DOT canonicalizes the same way
# (``parsed_allowed_uri.hostname in ("127.0.0.1", "::1")``, oauth2_provider/models.py:1483).
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class OAuthApplication(ExifStripMixin, AbstractApplication):  # type: ignore[misc]
    """A registered OAuth client. ``user`` (DOT's owner FK) is set for manual apps and None for dynamic ones."""

    IMAGE_FIELDS = ("logo",)

    #: Transient and never persisted: the plaintext client secret, attached for the one response
    #: that is allowed to show it (create and rotate-secret). It exists because ``client_secret``
    #: stores a *hash* — of the empty string for a public client — so a response schema that
    #: resolved that column would serialize the hash. Resolving a separate attribute makes that
    #: mistake impossible rather than merely unlikely.
    plaintext_client_secret: str | None = None

    algorithm = models.CharField(
        max_length=5, choices=AbstractApplication.ALGORITHM_TYPES, default=AbstractApplication.RS256_ALGORITHM
    )
    authorization_grant_type = models.CharField(
        max_length=44, choices=AbstractApplication.GRANT_TYPES, default=AbstractApplication.GRANT_AUTHORIZATION_CODE
    )
    description = models.TextField(blank=True)
    logo = models.ImageField(
        upload_to="oauth-logos",
        null=True,
        blank=True,
        validators=[FileExtensionValidator(allowed_extensions=ALLOWED_IMAGE_EXTENSIONS), validate_image_file],
    )
    logo_thumbnail = models.ImageField(
        max_length=255,
        blank=True,
        null=True,
        help_text="150x150 logo thumbnail (auto-generated).",
    )
    homepage_url = models.URLField(blank=True)
    privacy_policy_url = models.URLField(blank=True)
    allowed_scopes = models.JSONField(default=list, blank=True)
    verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta(AbstractApplication.Meta):  # type: ignore[misc]
        swappable = "OAUTH2_PROVIDER_APPLICATION_MODEL"

    def is_usable(self, request: t.Any) -> bool:
        """DOT consults this on authorize, token, refresh, userinfo and verify_request.

        Args:
            request: The ``oauthlib.common.Request`` being processed (unused).

        Returns:
            True while the application has not been deactivated.
        """
        return self.is_active

    def clean(self) -> None:
        """Revel rules on top of DOT's: owner by registration source, https/loopback URIs, known scopes.

        Raises:
            ValidationError: Keyed by field, so ``full_clean()`` reports each problem
                next to the input it belongs to.
        """
        super().clean()
        errors: dict[str, list[str]] = {}
        is_dynamic = self.registration_source == self.RegistrationSource.DCR
        if is_dynamic and self.user_id is not None:
            errors["user"] = [str(_("Dynamically registered apps have no owner."))]
        if not is_dynamic and self.user_id is None:
            errors["user"] = [str(_("An owner is required."))]
        # v1 ships only the authorization-code grant (spec, "out of scope"), but the field still
        # offers DOT's full GRANT_TYPES choice set, so admin and dynamic registration could both
        # pick another one. A client-credentials token in particular has no user, which makes the
        # OIDC claim builder raise at /userinfo. Note ``refresh_token`` is NOT a separate
        # authorization_grant_type in DOT (see ``test_refresh_token_is_not_a_separate_grant_type``),
        # so this does not disable refresh.
        if self.authorization_grant_type != self.GRANT_AUTHORIZATION_CODE:
            errors["authorization_grant_type"] = [
                str(_("Only the authorization-code grant is supported: {}").format(self.authorization_grant_type))
            ]
        # Only the plain-http/loopback rule below is genuinely ours: DOT allows http for every
        # client type once it is in ALLOWED_REDIRECT_URI_SCHEMES. The fragment and unknown-scheme
        # branches are unreachable while that setting stays ["https", "http"], because
        # ``super().clean()`` above runs DOT's AllowedURIValidator first and it rejects both
        # (oauth2_provider/validators.py:72-98). They are kept so the rule survives on our side if
        # that setting is ever widened.
        for uri in self.redirect_uris.strip().split():
            parsed = urlparse(uri)
            if parsed.fragment:
                errors.setdefault("redirect_uris", []).append(
                    str(_("Redirect URIs must not contain a fragment: {}").format(uri))
                )
            elif parsed.scheme == "http" and (
                self.client_type != self.CLIENT_PUBLIC or parsed.hostname not in _LOOPBACK_HOSTS
            ):
                errors.setdefault("redirect_uris", []).append(
                    str(_("Plain http is only allowed for loopback addresses of public clients: {}").format(uri))
                )
            elif parsed.scheme not in ("http", "https"):
                errors.setdefault("redirect_uris", []).append(str(_("Unsupported redirect URI scheme: {}").format(uri)))
        from oauth.scopes import SCOPES

        unknown = sorted(set(self.allowed_scopes or []) - set(SCOPES))
        if unknown:
            errors["allowed_scopes"] = [str(_("Unknown scopes: {}").format(", ".join(unknown)))]
        if errors:
            raise ValidationError(errors)

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Seed a dynamically registered app with the whole scope vocabulary (spec §6.1).

        RFC 7591 carries no per-app scope allowlist, so a dynamic client may *request* any
        registry scope and the consent screen is the real gate. Manual apps keep whatever
        their owner picked, including nothing.

        INSERT ONLY. On an update, an empty ``allowed_scopes`` means the scopes were
        deliberately revoked (the scope-shrink path, and the admin), and re-seeding there
        would invert a revocation into the widest possible grant. It would also have to write
        a field the caller's ``update_fields`` never named.
        """
        if self._state.adding and self.registration_source == self.RegistrationSource.DCR and not self.allowed_scopes:
            from oauth.scopes import SCOPES

            self.allowed_scopes = sorted(SCOPES)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return str(self.name or self.client_id)
