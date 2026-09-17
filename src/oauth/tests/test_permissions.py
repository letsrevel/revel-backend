"""Scope enforcement inside the keyed permission classes, plus ``RequireScope``.

An app token's effective power is *granted scopes ∩ the user's org permissions*
(spec §7.3). ``ScopedJWTAuth`` authenticates only; the scope half is
``scope_allows()``, which must run **before** any owner/creator short-circuit —
otherwise an org owner holding a narrowly-scoped app token would bypass scopes
entirely. Two tests below pin exactly that.
"""

import typing as t

import pytest
from django.http import HttpRequest
from django.test import RequestFactory
from ninja_extra.permissions import BasePermission

from accounts.models import RevelUser
from common.authentication import OAuthPrincipal
from events.controllers.permissions import (
    CanDuplicateEvent,
    EventPermission,
    EventSeriesPermission,
    ManagePotluckPermission,
    OrganizationPermission,
    QuestionnairePermission,
    scope_allows,
)
from events.models import Event, EventSeries, Organization, OrganizationQuestionnaire, PotluckItem
from oauth.exceptions import InsufficientScopeError
from oauth.permissions import RequireScope
from polls.models import Poll
from polls.permissions import PollPermission

pytestmark = pytest.mark.django_db

# The seven ``scope_allows`` gate sites (R-46): six in ``events.controllers.permissions``
# and ``polls.permissions.PollPermission``.
GATE_SITES = ("series", "event", "organization", "questionnaire", "duplicate_event", "potluck", "poll")


def _req(user: RevelUser, auth: t.Any) -> HttpRequest:
    request = RequestFactory().get("/")
    request.user = user
    request.auth = auth  # type: ignore[attr-defined]
    return request


@pytest.fixture
def oauth_potluck_item(user: RevelUser, oauth_event: Event) -> PotluckItem:
    """A potluck item created by ``user``, who is also the organization owner."""
    return PotluckItem.objects.create(
        event=oauth_event, name="Salad", created_by=user, item_type=PotluckItem.ItemTypes.FOOD
    )


def _gate_site(
    site: str, organization: Organization, oauth_event: Event, potluck_item: PotluckItem
) -> tuple[BasePermission, t.Any]:
    """Resolve ``site`` to a permission instance and an object its owner would otherwise pass.

    Unsaved model instances are deliberate where a row adds nothing: the gate raises
    before any attribute of ``obj`` is read. If a gate were missing, the permission
    would reach ``has_org_permission`` and return True for ``organization``'s owner,
    so the refusal test fails loudly rather than passing incidentally.
    """
    sites: dict[str, tuple[BasePermission, t.Any]] = {
        "series": (EventSeriesPermission("edit_event_series"), EventSeries(organization=organization)),
        "event": (EventPermission("edit_event"), oauth_event),
        "organization": (OrganizationPermission("manage_tickets"), organization),
        "questionnaire": (
            QuestionnairePermission("edit_questionnaire"),
            OrganizationQuestionnaire(organization=organization),
        ),
        "duplicate_event": (CanDuplicateEvent(), oauth_event),
        "potluck": (ManagePotluckPermission(), potluck_item),
        "poll": (PollPermission("manage_polls"), Poll(organization=organization)),
    }
    assert set(sites) == set(GATE_SITES)
    return sites[site]


def test_session_principal_passes_scope_check(user: RevelUser) -> None:
    scope_allows(_req(user, user), "manage_tickets")  # no raise


def test_missing_scope_raises(user: RevelUser) -> None:
    principal = OAuthPrincipal(client_id="c", scopes=frozenset({"org:read"}), token_id=1)
    with pytest.raises(InsufficientScopeError) as exc:
        scope_allows(_req(user, principal), "manage_tickets")
    assert exc.value.scope == "org:tickets"


def test_granted_scope_passes(user: RevelUser) -> None:
    principal = OAuthPrincipal(client_id="c", scopes=frozenset({"org:tickets"}), token_id=1)
    scope_allows(_req(user, principal), "manage_tickets")  # no raise


def test_unscoped_key_always_raises_for_app_tokens(user: RevelUser) -> None:
    principal = OAuthPrincipal(client_id="c", scopes=frozenset({"org:events", "org:read"}), token_id=1)
    with pytest.raises(InsufficientScopeError) as exc:
        scope_allows(_req(user, principal), "edit_organization")
    # R-45: an unscoped key names no scope in the challenge header — never the PermissionKey.
    assert exc.value.scope is None


def test_owner_short_circuit_does_not_bypass_scope(organization: Organization, oauth_event: Event) -> None:
    principal = OAuthPrincipal(client_id="c", scopes=frozenset({"org:read"}), token_id=1)
    with pytest.raises(InsufficientScopeError):
        EventPermission("edit_event").has_object_permission(_req(organization.owner, principal), None, oauth_event)  # type: ignore[arg-type]


def test_potluck_creator_short_circuit_does_not_bypass_scope(user: RevelUser, oauth_potluck_item: PotluckItem) -> None:
    principal = OAuthPrincipal(client_id="c", scopes=frozenset({"org:read"}), token_id=1)
    with pytest.raises(InsufficientScopeError):
        ManagePotluckPermission().has_object_permission(_req(user, principal), None, oauth_potluck_item)  # type: ignore[arg-type]


@pytest.mark.parametrize("site", GATE_SITES)
def test_every_gate_site_refuses_an_unscoped_app_token(
    site: str, organization: Organization, oauth_event: Event, oauth_potluck_item: PotluckItem
) -> None:
    """Each of the seven keyed permission classes refuses a token lacking its scope."""
    permission, obj = _gate_site(site, organization, oauth_event, oauth_potluck_item)
    principal = OAuthPrincipal(client_id="c", scopes=frozenset({"openid"}), token_id=1)
    with pytest.raises(InsufficientScopeError):
        permission.has_object_permission(_req(organization.owner, principal), None, obj)  # type: ignore[arg-type]


@pytest.mark.parametrize("site", GATE_SITES)
def test_every_gate_site_is_a_no_op_for_session_principals(
    site: str, organization: Organization, oauth_event: Event, oauth_potluck_item: PotluckItem
) -> None:
    """Session-authenticated requests keep their pre-existing behaviour: the owner passes."""
    permission, obj = _gate_site(site, organization, oauth_event, oauth_potluck_item)
    owner = organization.owner
    assert permission.has_object_permission(_req(owner, owner), None, obj) is True  # type: ignore[arg-type]


def test_require_scope(user: RevelUser) -> None:
    perm = RequireScope("me:read")
    assert perm.has_permission(_req(user, user), None) is True  # type: ignore[arg-type]
    assert perm.has_permission(_req(user, OAuthPrincipal("c", frozenset({"me:read"}), 1)), None) is True  # type: ignore[arg-type]
    with pytest.raises(InsufficientScopeError) as exc:
        perm.has_permission(_req(user, OAuthPrincipal("c", frozenset({"org:read"}), 1)), None)  # type: ignore[arg-type]
    assert exc.value.scope == "me:read"


def test_require_scope_object_permission_delegates(user: RevelUser) -> None:
    perm = RequireScope("me:read")
    assert perm.has_object_permission(_req(user, user), None, object()) is True  # type: ignore[arg-type]
    with pytest.raises(InsufficientScopeError):
        perm.has_object_permission(_req(user, OAuthPrincipal("c", frozenset(), 1)), None, object())  # type: ignore[arg-type]
