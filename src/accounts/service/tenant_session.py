"""Bind a session to the tenant host (custom domain) it was issued on (white-label, #1148).

Logging in on a tenant host stores the user's platform-wide refresh token as a host-only cookie on
that host. The organization owner controls the host's DNS, so if they repoint it at their own server
the browser hands them that cookie. To make such a session die with the domain, the refresh token
issued on a tenant host carries two claims, copied into every access token derived from it:

* ``tenant_host``: the hostname the session was issued on;
* ``tenant_session_iat``: unix time (int) the *session* was issued. Rotation carries it forward
  unchanged, so it dates the login, not the latest refresh.

A token without ``tenant_host`` is a platform session and is always valid here.

Validity of a tenant session is checked against live state, in two tiers:

* every request (access-token auth) and every refresh: the host must currently resolve to an ACTIVE
  domain of an organization whose ``custom_domain_enabled`` kill switch is on. That is the cached
  tenant resolution (``tenant_service``, at most ``POSITIVE_TTL`` seconds stale) and costs no query.
  Removal, org deletion, the kill switch and a FAILED domain all stop the host resolving.
* refresh only: ``tenant_session_iat`` must not predate the domain's ``verified_at``. The cached
  resolution holds an organization id and not the timestamp, so this half costs one query and is not
  paid per request. It kills sessions minted before the hostname was re-activated or re-claimed;
  an access token of such a session lives at most ``ACCESS_TOKEN_LIFETIME`` longer, and it cannot be
  refreshed. The comparison is in whole seconds, so a session minted in the same second as a
  re-activation survives.
"""

import time
import typing as t

from ninja_jwt.exceptions import InvalidToken, TokenError
from ninja_jwt.schema import TokenObtainPairOutputSchema
from ninja_jwt.tokens import RefreshToken

from events.models import OrganizationDomain
from events.service.tenant_service import is_tenant_origin

TENANT_HOST_CLAIM = "tenant_host"
TENANT_SESSION_IAT_CLAIM = "tenant_session_iat"


def tenant_claims(tenant_host: str) -> dict[str, t.Any]:
    """Return the claims that bind a freshly issued session to ``tenant_host``."""
    return {TENANT_HOST_CLAIM: tenant_host, TENANT_SESSION_IAT_CLAIM: int(time.time())}


def restamp_pair_for_tenant(pair: TokenObtainPairOutputSchema, tenant_host: str) -> None:
    """Bind an already minted token pair to ``tenant_host``, in place.

    Used for the stock ninja_jwt password pair, whose ``get_token`` builds the tokens without the
    request. The refresh token is re-signed with the claims added (same ``jti``, ``exp`` and payload
    otherwise) and the access token derived from it again.
    """
    refresh = RefreshToken(pair.refresh, verify=False)
    refresh.payload.update(tenant_claims(tenant_host))
    pair.refresh = str(refresh)
    pair.access = str(refresh.access_token)


def is_tenant_session_valid(payload: t.Mapping[str, t.Any], *, check_reactivation: bool = False) -> bool:
    """Whether the session a token payload belongs to may still be used.

    Args:
        payload: The decoded claims of an access or refresh token.
        check_reactivation: Also require the session to postdate the domain's ``verified_at`` (one
            query). Done on refresh only, see the module docstring.

    Returns:
        bool: ``True`` for a platform session (no ``tenant_host`` claim), or a tenant session whose
        host still resolves to an active domain (and, if asked, was not re-activated since).
    """
    if TENANT_HOST_CLAIM not in payload:
        return True
    host = payload[TENANT_HOST_CLAIM]
    issued = payload.get(TENANT_SESSION_IAT_CLAIM)
    if not isinstance(host, str) or not isinstance(issued, int) or isinstance(issued, bool):
        return False
    if not is_tenant_origin(host):
        return False
    if not check_reactivation:
        return True
    verified_at = (
        OrganizationDomain.objects.filter(hostname=host, status=OrganizationDomain.Status.ACTIVE)
        .values_list("verified_at", flat=True)
        .first()
    )
    return verified_at is not None and issued >= int(verified_at.timestamp())


def revoke_refresh_if_tenant_session_invalid(raw_refresh: str) -> None:
    """Refuse a refresh token whose tenant session is no longer valid, blacklisting it.

    A token that does not decode (expired, forged, already blacklisted) is left to the stock refresh
    flow, which answers it as it always did.

    Raises:
        InvalidToken: The session is revoked (401, the shape ninja_jwt uses for any invalid token).
    """
    try:
        refresh = RefreshToken(raw_refresh)
    except TokenError:
        return
    if is_tenant_session_valid(refresh.payload, check_reactivation=True):
        return
    refresh.blacklist()
    raise InvalidToken
