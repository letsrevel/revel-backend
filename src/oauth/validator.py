"""DOT validator subclass: id_token/userinfo claims and OIDC-correct ``offline_access`` (spec §10)."""

import typing as t

from django.conf import settings
from oauth2_provider.oauth2_validators import OAuth2Validator

from accounts.models import RevelUser
from common.signing import get_file_url


def _picture_url(user: RevelUser) -> str | None:
    """Absolute, signed URL for ``user``'s avatar thumbnail, or ``None`` when there is none.

    Thumbnails live under ``protected/``, so ``get_file_url`` signs them; an unsigned path is
    refused at the edge and would make the claim a lie. ``get_file_url`` returns a
    root-relative path while an OIDC ``picture`` claim must be a URL, so it is joined onto the
    issuer (the API origin, which is also where the media is served from).

    Args:
        user: The end user whose claims are being built.

    Returns:
        The avatar URL, or ``None`` when the user has no thumbnail.
    """
    path = get_file_url(user.profile_picture_thumbnail)
    if not path:
        return None
    # ponytail: the signature is valid for ``common.signing.DEFAULT_EXPIRES_IN`` (one hour).
    # That happens to equal ID_TOKEN_EXPIRE_SECONDS today, but the two are independent
    # literals and NOT coupled: ``get_file_url`` takes no lifetime override, so raising the
    # token lifetime would leave the claim outliving its URL. A client that caches the claim
    # past the hour must re-read userinfo. Coupling them means either teaching
    # ``get_file_url`` an ``expires_in`` passthrough or inlining its protected-path branch
    # here; a permanently fetchable avatar would need an unprotected, immutable thumbnail copy.
    return f"{settings.OAUTH_ISSUER}{path}"


class RevelOAuth2Validator(OAuth2Validator):  # type: ignore[misc]
    """Scope-gated OIDC claims and an ``offline_access``-gated refresh token."""

    # DOT filters both the id_token and the userinfo response through this map, dropping any
    # claim whose key is absent from it, so it must agree key for key with the dict
    # ``get_additional_claims`` builds (``test_claim_scope_map_matches_emitted_claims``).
    # ``preferred_username`` is deliberately absent: ``username`` IS the email on every
    # creation path in this product, so emitting it would hand a ``profile``-only app the
    # address the separate ``email`` scope exists to gate (R-56). OIDC makes the claim
    # optional and RPs identify users by ``sub``.
    oidc_claim_scope = {
        "sub": "openid",
        "name": "profile",
        "picture": "profile",
        "locale": "profile",
        "email": "email",
        "email_verified": "email",
    }

    def get_additional_claims(self, request: t.Any) -> dict[str, t.Any]:
        """Build the claims granted by ``request``'s scopes.

        These claims leave the system to a third-party app, so ``is_staff``, ``is_superuser``
        and group membership are never emitted (spec §9).

        Args:
            request: The oauthlib request, carrying the end user and the granted scopes.

        Returns:
            The claims to merge into the id_token and the userinfo response.
        """
        user: RevelUser = request.user
        scopes = set(request.scopes)
        claims: dict[str, t.Any] = {"sub": str(user.pk)}
        if "profile" in scopes:
            # NOT ``get_display_name()``: its last fallback is ``username``, i.e. the email
            # (R-56 amends R-52). Both components below are blank-able, so the claim is
            # omitted rather than guessed — better no name than an address.
            claims["name"] = user.preferred_name or user.get_full_name() or None
            claims["locale"] = user.language  # non-blank, defaults to LANGUAGE_CODE
            claims["picture"] = _picture_url(user)
        # ``email`` is blank-able on AbstractUser, and an empty claim means nothing to a
        # client, so the pair is emitted together or not at all.
        if "email" in scopes and user.email:
            claims["email"] = user.email
            claims["email_verified"] = user.email_verified
        return {k: v for k, v in claims.items() if v is not None}

    def save_bearer_token(self, token: dict[str, t.Any], request: t.Any, *args: t.Any, **kwargs: t.Any) -> None:
        """Drop the refresh token unless ``offline_access`` was granted.

        The pop happens *before* ``super()`` on purpose: oauthlib hands us the very dict it
        serialises into the token response (``create_token`` → ``save_token`` → ``json.dumps``),
        and DOT's ``_save_bearer_token`` persists a ``RefreshToken`` row only when the key is
        present. Popping afterwards would store a refresh token and merely hide it from the
        response, which is worse than either alternative.

        One documented side effect, deliberately left alone: if a client holding
        ``offline_access`` refreshes while *narrowing* the scope (e.g. ``scope=openid``), the
        pop makes ``refresh_token_code`` falsy, so DOT skips its whole ``if refresh_token_code:``
        block — including ``refresh_token_instance.revoke()`` — and the presented refresh token
        stays live. Nothing new is issued, and RFC 6749 §6 permits a narrowed refresh, so the
        gate's property holds; it is simply not a rotation.

        Args:
            token: The token dict oauthlib built and will serialise.
            request: The oauthlib request, carrying the granted scopes.
            *args: Passed through to DOT.
            **kwargs: Passed through to DOT.
        """
        if "offline_access" not in (request.scopes or []):
            token.pop("refresh_token", None)
        super().save_bearer_token(token, request, *args, **kwargs)
