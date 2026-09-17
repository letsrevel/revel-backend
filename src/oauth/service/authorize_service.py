"""Headless consent (spec §8.2): validate with DOT's ``OAuthLibCore``, decide, return a redirect.

DOT's session-based ``AuthorizationView`` is never mounted, so this module is its headless
equivalent: the SvelteKit page at ``FRONTEND_BASE_URL/oauth/authorize`` (which both discovery
documents advertise as the ``authorization_endpoint``) calls ``describe`` to render the screen
and ``decide`` to act on the answer.

The authorization request itself is still validated by oauthlib through DOT's core, so client,
redirect URI, PKCE and scope rules are exactly the ones the token endpoint will enforce later.
"""

import hashlib
import typing as t
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.models import AccessToken, RefreshToken
from oauth2_provider.oauth2_backends import get_oauthlib_core
from oauth2_provider.oauth2_validators import is_valid_resource_uri

from accounts.models import RevelUser
from oauth.exceptions import AuthorizationRequestError, OAuthProviderDisabledError
from oauth.models import OAuthApplication
from oauth.utils import oauth_provider_enabled

# How long the consent screen stays answerable. It only has to cover the round trip from
# rendering the screen to the user clicking a button, so it is deliberately minutes rather
# than the hour ``common.signing`` defaults to for media URLs: a stale screen must be
# re-fetched (and the scopes re-read) rather than silently answered.
CONSENT_TICKET_TTL_SECONDS = 300

# Domain separator, so a ticket cannot be replayed as any other signed value in the product.
_CONSENT_TICKET_SALT = "revel:oauth-consent-ticket:v1"


@dataclass(frozen=True)
class AuthorizeRedirect:
    """A ready-made URL for the browser: a success redirect or a client-visible error."""

    redirect_to: str


@dataclass(frozen=True)
class AuthorizeDescription:
    """Everything the consent screen has to show before the user can answer."""

    application: OAuthApplication
    scopes: list[str]
    redirect_uri: str
    state: str | None
    consent_ticket: str


def _ensure_enabled() -> None:
    """Refuse every consent call while the provider is switched off (ADR-0008).

    Raises:
        OAuthProviderDisabledError: No signing key is configured; rendered as a 404.
    """
    if not oauth_provider_enabled():
        raise OAuthProviderDisabledError()


def _as_error(exc: OAuthToolkitError) -> AuthorizationRequestError:
    """Convert DOT's wrapper into the app exception ``oauth.exception_handlers`` renders.

    ``err.description`` is passed through untranslated on purpose (R-66): it is authored by
    oauthlib at runtime, so ``gettext`` could never see it, and RFC 6749 §4.1.2.1 defines
    ``error_description`` as developer-facing text "used to assist the client developer in
    understanding the error". oauthlib leaves some errors (``invalid_scope``) with no
    description at all, hence the error code as the last resort.

    Args:
        exc: The error DOT's core raised.

    Returns:
        The equivalent ``AuthorizationRequestError``.
    """
    err = exc.oauthlib_error
    return AuthorizationRequestError(err.error, err.description or err.error)


def _resource_indicators(request: HttpRequest) -> list[str]:
    """The validated RFC 8707 ``resource`` values from the authorization request.

    oauthlib knows nothing about resource indicators, so the raw query value survives on the
    oauthlib request as a *string* and DOT's ``ResourceJSONField`` then refuses to store it
    ("Resource must be a list of URI strings"), which 500s the whole flow. DOT's own
    ``AuthorizationView`` normalises and validates the parameter before it reaches the grant;
    this is that step, headless. RFC 8707 allows the parameter to repeat.

    Args:
        request: The authorization request.

    Returns:
        The resource indicators, or an empty list when the client sent none.

    Raises:
        AuthorizationRequestError: A value is not an absolute URI with a scheme and host.
    """
    resources = request.GET.getlist("resource")
    invalid = [uri for uri in resources if not is_valid_resource_uri(uri)]
    if invalid:
        raise AuthorizationRequestError(
            "invalid_target",
            str(_("Not a valid resource indicator: {}").format(invalid[0])),
        )
    return resources


def _validate(request: HttpRequest) -> tuple[list[str], dict[str, t.Any]]:
    """Run oauthlib's authorization-request validation and return its credentials.

    Args:
        request: The authorization request.

    Returns:
        The requested scopes and the credentials oauthlib built from the request.

    Raises:
        AuthorizationRequestError: The request is invalid. Both fatal errors (unknown client,
            bad redirect URI) and redirectable ones (``invalid_scope``, missing PKCE) are
            reported to the *frontend* as a 400 carrying the RFC 6749 code, rather than
            redirected to the client.
            # ponytail: RFC 6749 §4.1.2.1 says a non-fatal error SHOULD be redirected to the
            # client instead. Doing that needs a fatal/non-fatal split here
            # (``FatalClientError`` is a subclass of ``OAuthToolkitError``) plus a frontend
            # that follows a redirect it did not ask for; until the consent page needs it,
            # the error stays visible to the user who is standing in front of it.
    """
    try:
        scopes, credentials = get_oauthlib_core().validate_authorization_request(request)
    except OAuthToolkitError as exc:
        raise _as_error(exc) from exc
    credentials = dict(credentials)
    resources = _resource_indicators(request)
    if resources:
        credentials["resource"] = resources
    return list(scopes), credentials


def _error_redirect(redirect_uri: str, error: str, state: str | None) -> AuthorizeRedirect:
    """Add an RFC 6749 error (and the client's ``state``) to an already-validated redirect URI.

    Round-tripped through ``parse_qsl``/``urlencode`` rather than a hand-rolled split (R-67):
    a registered redirect URI may carry a query string of its own (RFC 6749 §3.1.2, which
    ``OAuthApplication.clean`` deliberately permits), and splitting on ``=`` would
    double-encode its values and mangle any containing ``=`` or ``&``.

    Args:
        redirect_uri: The redirect URI oauthlib has already validated for this client.
        error: The RFC 6749 / OIDC error code.
        state: The client's CSRF value, echoed back when it sent one.

    Returns:
        The redirect the browser must follow.
    """
    parts = urlsplit(redirect_uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append(("error", error))
    if state:
        query.append(("state", state))
    return AuthorizeRedirect(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")))


def has_prior_grant(user: RevelUser, application: OAuthApplication, scopes: list[str], resources: list[str]) -> bool:
    """Whether ``user`` already granted ``application`` at least this much authority.

    Widens DOT's own auto-approve rule (a live access token covering the request) with live
    refresh tokens, so a client holding ``offline_access`` is not re-prompted merely because
    its access token expired. Scopes come from the raw ``scope`` string, not the ``scopes``
    property, so the answer reflects what was granted at issue time — the same choice
    ``ScopedJWTAuth`` makes.

    Coverage is over scopes **and** RFC 8707 resource indicators (R-75). An empty resource
    set is the *universal* set — an unrestricted token is usable at every resource — so the
    rule is not a plain subset test in both directions: an unrestricted grant covers any
    request, while a request that asks for no resource is only covered by an equally
    unrestricted grant. Without that asymmetry a grant obtained for ``resource=https://a``
    would silently auto-approve a re-authorization with no ``resource`` at all, minting a
    token with strictly more power than the user ever consented to.

    Args:
        user: The end user answering the consent screen.
        application: The client asking for authorization.
        scopes: The scopes this request asks for.
        resources: The resource indicators this request asks for; empty means unrestricted.

    Returns:
        True when some live grant covers both the requested scopes and the requested audience.
    """
    wanted_scopes = set(scopes)
    wanted_resources = set(resources)
    granted = list(
        AccessToken.objects.filter(user=user, application=application, expires__gt=timezone.now()).values_list(
            "scope", "resource"
        )
    )
    granted += RefreshToken.objects.filter(
        user=user, application=application, revoked__isnull=True, access_token__isnull=False
    ).values_list("access_token__scope", "access_token__resource")
    for granted_scope, granted_resource in granted:
        if not wanted_scopes <= set(granted_scope.split()):
            continue
        granted_resources = set(granted_resource or [])
        if granted_resources and not (wanted_resources and wanted_resources <= granted_resources):
            continue
        return True
    return False


def _consent_fingerprint(user: RevelUser, scopes: list[str], credentials: dict[str, t.Any]) -> str:
    """Canonical digest of everything the consent screen told the user it was granting.

    Sorted, newline-joined and hashed so the ticket stays short and opaque while still
    binding the decision to one user, one client, one redirect URI, one displayed scope set,
    one PKCE challenge and one audience. ``state`` is deliberately absent: it is the client's
    own CSRF value and not part of what the user is consenting to.

    Args:
        user: The end user the screen was rendered for.
        scopes: The scopes the screen displayed.
        credentials: The credentials ``_validate`` returned for the request.

    Returns:
        A hex digest.
    """
    parts = [
        str(user.pk),
        str(credentials["client_id"]),
        str(credentials["redirect_uri"]),
        " ".join(sorted(scopes)),
        str(credentials.get("code_challenge") or ""),
        " ".join(sorted(credentials.get("resource") or [])),
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _sign_consent_ticket(user: RevelUser, scopes: list[str], credentials: dict[str, t.Any]) -> str:
    """Mint the ticket that proves this user was shown exactly these scopes.

    Args:
        user: The end user the screen is being rendered for.
        scopes: The scopes the screen displays.
        credentials: The credentials ``_validate`` returned.

    Returns:
        The opaque ticket the frontend must echo back with the decision.
    """
    signer = TimestampSigner(salt=_CONSENT_TICKET_SALT)
    return signer.sign(_consent_fingerprint(user, scopes, credentials))


def _check_consent_ticket(
    ticket: str | None, user: RevelUser, scopes: list[str], credentials: dict[str, t.Any]
) -> None:
    """Refuse an approval that is not backed by a consent screen this user was actually shown.

    The session JWT travels in a header, so there is no cookie to ride and no classic CSRF
    here — but without this check an XSS or a malicious extension on the frontend origin
    could POST ``allow=True`` with wider scopes than the screen displayed, and the backend
    would have no way to tell. The ticket is the backend's own proof, so "the user saw what
    they granted" stops being a frontend-only guarantee (R-74).

    Args:
        ticket: The ticket the client echoed back, if any.
        user: The end user approving.
        scopes: The scopes this POST is asking to grant.
        credentials: The credentials ``_validate`` returned for this POST.

    Raises:
        AuthorizationRequestError: The ticket is missing, expired, forged, or describes a
            different grant than this request. ``consent_required`` means "show the screen
            again"; ``invalid_request`` means the decision did not match the consent and
            retrying it unchanged will not help.
    """
    if not ticket:
        raise AuthorizationRequestError(
            "consent_required", str(_("A consent ticket from the authorization screen is required."))
        )
    signer = TimestampSigner(salt=_CONSENT_TICKET_SALT)
    try:
        signed = signer.unsign(ticket, max_age=CONSENT_TICKET_TTL_SECONDS)
    except SignatureExpired as exc:
        raise AuthorizationRequestError(
            "consent_required", str(_("The authorization screen expired. Please review the request again."))
        ) from exc
    except BadSignature as exc:
        raise AuthorizationRequestError("invalid_request", str(_("Invalid consent ticket."))) from exc
    if signed != _consent_fingerprint(user, scopes, credentials):
        raise AuthorizationRequestError(
            "invalid_request", str(_("This decision does not match the authorization that was shown."))
        )


def _issue(request: HttpRequest, scopes: list[str], credentials: dict[str, t.Any], *, allow: bool) -> AuthorizeRedirect:
    """Hand the decision to oauthlib and return wherever the browser has to go.

    DOT reads the consenting end user off ``request.user``, which the auth class has set.

    Args:
        request: The authorization request.
        scopes: The scopes being granted.
        credentials: The credentials ``_validate`` returned.
        allow: The user's decision.

    Returns:
        The client's redirect URI carrying either a code or an error (a refusal becomes
        ``access_denied``, which belongs in the redirect rather than in our response body).
    """
    try:
        uri, _headers, _body, _status = get_oauthlib_core().create_authorization_response(
            request, scopes, credentials, allow
        )
    except OAuthToolkitError as exc:
        # DOT copies ``credentials["redirect_uri"]`` onto every error raised here
        # (``oauth2_backends.create_authorization_response``), and ``_validate`` has already
        # proved that URI belongs to this client, so there is always somewhere to send it.
        # ponytail: a *fatal* error (``FatalClientError``, e.g. the app is deleted in the
        # window between validation and issuance) is redirected too, where RFC 6749 §4.1.2.1
        # says it should not be. Splitting the two costs an ``except FatalClientError`` clause
        # above this one; it is not done yet because the only way to reach it is that race,
        # nothing is disclosed (the URI was validated microseconds earlier), and a redirect is
        # the better answer for the user either way.
        err = exc.oauthlib_error
        return AuthorizeRedirect(t.cast(str, err.in_uri(err.redirect_uri)))
    return AuthorizeRedirect(t.cast(str, uri))


def describe(request: HttpRequest, user: RevelUser) -> AuthorizeDescription | AuthorizeRedirect:
    """Validate the authorization request, then auto-approve it or describe the consent screen.

    Args:
        request: The authorization request, with its parameters in the query string.
        user: The signed-in end user.

    Returns:
        An ``AuthorizeRedirect`` when no interaction is needed (a trusted app, a prior grant,
        or a ``prompt=none`` request that cannot be satisfied silently), otherwise the
        description the consent screen renders, carrying the ticket the decision must echo.
    """
    _ensure_enabled()
    scopes, credentials = _validate(request)
    application = t.cast(OAuthApplication, credentials["request"].client)
    redirect_uri = t.cast(str, credentials["redirect_uri"])
    state = t.cast("str | None", credentials.get("state"))
    # OIDC Core §3.1.2.1. ``prompt=login`` is not honoured: the end user is already
    # authenticated by the time the frontend can call this, and re-authentication is a
    # frontend concern (it owns the session), so it is treated as a plain consent request.
    prompt = set(request.GET.get("prompt", "").split())
    auto = application.skip_authorization or has_prior_grant(
        user, application, scopes, t.cast(list[str], credentials.get("resource") or [])
    )
    # No consent ticket on this branch, and none is needed: no screen is rendered, so there is
    # nothing to bind a decision to. The authority comes from elsewhere entirely —
    # ``skip_authorization`` is an operator-set flag on the app, and ``has_prior_grant``
    # requires a live grant from this same user to this same app already covering these
    # scopes and this audience. Neither can be influenced by whoever made the call.
    if "consent" not in prompt and auto:
        return _issue(request, scopes, credentials, allow=True)
    if "none" in prompt:
        return _error_redirect(redirect_uri, "interaction_required", state)
    return AuthorizeDescription(
        application=application,
        scopes=scopes,
        redirect_uri=redirect_uri,
        state=state,
        consent_ticket=_sign_consent_ticket(user, scopes, credentials),
    )


def decide(request: HttpRequest, user: RevelUser, *, allow: bool, consent_ticket: str | None) -> AuthorizeRedirect:
    """Apply the user's answer; a refusal yields the client's ``access_denied`` redirect.

    Args:
        request: The authorization request, replayed in the query string of the decision POST.
        user: The signed-in end user, whose identity the consent ticket is bound to.
        allow: The user's answer. There is no default — no branch of this module issues a code
            without either an explicit ``True`` here or a genuine prior grant in ``describe``.
        consent_ticket: The ticket ``describe`` handed the screen. Required to approve;
            ignored for a refusal, which issues no code and can therefore be honoured from a
            screen that has since expired.

    Returns:
        The redirect the browser must follow.
    """
    _ensure_enabled()
    scopes, credentials = _validate(request)
    if allow:
        _check_consent_ticket(consent_ticket, user, scopes, credentials)
    return _issue(request, scopes, credentials, allow=allow)
