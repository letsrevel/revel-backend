"""Wire schemas for the oauth app."""

import typing as t

from django.conf import settings
from ninja import Schema

from common.signing import get_file_url
from oauth.models import OAuthApplication
from oauth.scopes import SCOPES, Group


def app_logo_url(application: OAuthApplication) -> str | None:
    """Absolute, fetchable URL for ``application``'s logo thumbnail, or ``None``.

    NOT ``logo_thumbnail.url``: thumbnails can live under ``protected/``, where Caddy's
    ``forward_auth`` refuses an unsigned path, and the raw ``.url`` is root-relative while
    the consent screen is served from a different origin. ``get_file_url`` signs when the
    path is protected and the issuer (the API origin, which also serves the media) makes it
    absolute — the same treatment ``oauth.validator`` gives the OIDC ``picture`` claim.

    Args:
        application: The registered client whose logo is being rendered.

    Returns:
        The absolute logo URL, or ``None`` when the app has no thumbnail.
    """
    path = get_file_url(application.logo_thumbnail)
    if not path:
        return None
    return f"{settings.OAUTH_ISSUER}{path}"


class AuthorizeAppSchema(Schema):
    """The client as the consent screen and the Connected Apps screen show it."""

    name: str
    description: str
    logo_url: str | None = None
    verified: bool
    # The enum is referenced from the model per project convention, but it is inherited from
    # DOT's ``AbstractApplication``, which ``disallow_subclassing_any`` makes ``Any`` — so
    # mypy cannot see the nested class as a *type* even though the runtime value is correct.
    # The alternative is re-declaring DOT's vocabulary here, which would silently drift.
    registration_source: OAuthApplication.RegistrationSource  # type: ignore[name-defined]
    homepage_url: str
    privacy_policy_url: str

    @classmethod
    def from_app(cls, application: OAuthApplication) -> "AuthorizeAppSchema":
        """Render ``application`` for the consent and connections responses.

        Args:
            application: The registered client to describe.

        Returns:
            The wire representation of the client.
        """
        return cls(
            name=application.name,
            description=application.description,
            logo_url=app_logo_url(application),
            verified=application.verified,
            registration_source=OAuthApplication.RegistrationSource(application.registration_source),
            homepage_url=application.homepage_url,
            privacy_policy_url=application.privacy_policy_url,
        )


class AuthorizeScopeSchema(Schema):
    """One consent-screen row: what the app is asking for, and which section it belongs to."""

    name: str
    label: str
    group: Group

    @classmethod
    def rows(cls, scopes: list[str]) -> list["AuthorizeScopeSchema"]:
        """Describe ``scopes`` in the order the client asked for them.

        Deliberately unguarded: ``RegistryScopes.get_available_scopes`` already intersects the
        app's ``allowed_scopes`` with ``SCOPES``, so a validated scope is always known here.
        Skipping an unknown one would hide from the user something the token still carries, so
        a broken invariant must raise rather than quietly shorten the consent screen.

        Args:
            scopes: Scope names, as validated against the app's ``allowed_scopes``.

        Returns:
            One row per requested scope.
        """
        return [cls(name=name, label=str(SCOPES[name].label), group=SCOPES[name].group) for name in scopes]


class AuthorizeDescribeResponse(Schema):
    """What the frontend needs to render the consent screen."""

    application: AuthorizeAppSchema
    scopes: list[AuthorizeScopeSchema]
    redirect_uri: str
    state: str | None = None
    #: Opaque, short-lived proof that this user was shown exactly these scopes. The consent
    #: page must echo it back as ``consent_ticket`` when the user approves; see
    #: ``oauth.service.authorize_service.CONSENT_TICKET_TTL_SECONDS``.
    consent_ticket: str


class AuthorizeRedirectResponse(Schema):
    """Where the browser must go next: the client's redirect URI, already built."""

    redirect_to: str


class AuthorizeDecisionPayload(Schema):
    """The user's answer to the consent screen. Required: there is no implicit approval."""

    allow: bool
    #: The ticket from ``AuthorizeDescribeResponse``. Required when ``allow`` is true and
    #: ignored otherwise — a refusal issues no code, so it is still honoured from a screen
    #: that has expired in the meantime.
    consent_ticket: str | None = None


AuthorizeResponse: t.TypeAlias = AuthorizeDescribeResponse | AuthorizeRedirectResponse
