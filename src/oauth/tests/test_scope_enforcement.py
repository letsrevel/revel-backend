"""End-to-end scope enforcement, one representative route per ``org:`` scope (spec §15).

For each scope: a token carrying it reaches the handler (a **concrete** success status — a
404 would mean the path is wrong and must fail, R-91), and a token carrying any *other*
``org:`` scope is refused **403 with an ``insufficient_scope`` challenge**.

Two properties this pins that the unit tests cannot:

* the scope gate really is wired into the live route tree, not just into the permission
  classes; and
* an insufficient *scope* (403 + ``insufficient_scope``) is distinguishable from an
  insufficient *org permission* (403 without it) — the last test.

``org:read`` is special-cased: ``view_organization_details`` is a ``PermissionKey`` that no
route and no permission class uses (R-30), so ``scope_allows()`` can never enforce it. Its
only enforcement is ``RequireScope("org:read")`` on the switched controllers, and that is
what the test exercises — no route is invented for it.
"""

import datetime as dt
import json
import typing as t

import pytest
from django.test.client import Client
from django.utils import timezone

from accounts.models import RevelUser
from events.models import (
    EventSeries,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    PermissionMap,
    PermissionsSchema,
)
from oauth.models import OAuthApplication
from oauth.scopes import SCOPES
from oauth.tests.test_auth_class import make_access_token

pytestmark = pytest.mark.django_db

ORG_SCOPES = frozenset(name for name in SCOPES if name.startswith("org:"))

# The one route whose scope requirement comes from ``RequireScope`` alone (R-30).
ORG_READ_ROUTE = "/api/dashboard/organizations"


class Case(t.NamedTuple):
    """One representative route for a scope."""

    method: str
    path: str
    body: dict[str, t.Any] | None
    expected: int


def _cases(
    organization: Organization,
    event: t.Any,
    member: OrganizationMember,
    org_questionnaire: t.Any,
    poll: t.Any,
    potluck: t.Any,
) -> dict[str, Case]:
    """The route table, keyed by the scope whose ``PermissionKey`` gates each route.

    Every path here was read off the live route tree, and every ``expected`` status is a
    success: the owner of ``organization`` holds every org permission, so the only thing
    that can refuse these requests is the scope gate.
    """
    slug, event_id = organization.slug, event.id
    return {
        # OrganizationPermission("create_event") via CanDuplicateEvent.
        "org:events": Case(
            "POST",
            f"/api/event-admin/{event_id}/duplicate",
            {"name": "Duplicate", "start": (timezone.now() + dt.timedelta(days=7)).isoformat()},
            200,
        ),
        # EventPermission("manage_tickets").
        "org:tickets": Case("GET", f"/api/event-admin/{event_id}/tickets", None, 200),
        # OrganizationPermission("check_in_attendees") — a real membership card scan.
        "org:checkin": Case("GET", f"/api/organization-admin/{slug}/members/verify/{member.qr_payload}", None, 200),
        # OrganizationPermission("manage_members").
        "org:members": Case("GET", f"/api/organization-admin/{slug}/staff", None, 200),
        # OrganizationPermission("send_announcements").
        "org:announcements": Case("GET", f"/api/organization-admin/{slug}/announcements", None, 200),
        # QuestionnairePermission("evaluate_questionnaire").
        "org:questionnaires": Case("GET", f"/api/questionnaires/{org_questionnaire.id}", None, 200),
        # PollPermission("manage_polls") on PollQuestionController (NOT PollController, which
        # stays OptionalAuth for anonymous poll reads — R-05).
        "org:polls": Case("POST", f"/api/polls/{poll.id}/sections", {"name": "Section"}, 200),
        # ManagePotluckPermission() — "manage_potluck".
        "org:potluck": Case(
            "PUT",
            f"/api/events/{event_id}/potluck/{potluck.id}",
            {"name": "Renamed", "item_type": potluck.item_type},
            200,
        ),
    }


def _request(user: RevelUser, app: OAuthApplication, scopes: str, case: Case) -> t.Any:
    """Issue ``case`` with an app token granting exactly ``scopes``."""
    client = Client(HTTP_AUTHORIZATION=f"Bearer {make_access_token(user, app, scopes)}")
    return client.generic(
        case.method,
        case.path,
        data=json.dumps(case.body) if case.body is not None else "",
        content_type="application/json",
    )


@pytest.fixture
def cases(
    organization: Organization,
    oauth_event: t.Any,
    oauth_org_member: OrganizationMember,
    oauth_org_questionnaire: t.Any,
    oauth_poll: t.Any,
    oauth_potluck: t.Any,
) -> dict[str, Case]:
    """The route table, built against this test package's own ``oauth_``-prefixed fixtures."""
    return _cases(organization, oauth_event, oauth_org_member, oauth_org_questionnaire, oauth_poll, oauth_potluck)


@pytest.mark.parametrize("scope", sorted(ORG_SCOPES - {"org:read"}))
def test_only_the_matching_scope_passes(
    scope: str, cases: dict[str, Case], organization: Organization, oauth_app: OAuthApplication
) -> None:
    """The scope's own route succeeds; every other ``org:`` scope is refused with a challenge."""
    case = cases[scope]
    owner = organization.owner
    granted = _request(owner, oauth_app, f"org:read {scope}", case)
    assert granted.status_code == case.expected, (scope, granted.status_code, granted.content)

    for other in sorted(ORG_SCOPES - {scope, "org:read"}):
        denied = _request(owner, oauth_app, f"org:read {other}", case)
        assert denied.status_code == 403, (scope, other, denied.status_code, denied.content)
        assert "insufficient_scope" in denied["WWW-Authenticate"], (scope, other, denied["WWW-Authenticate"])


def test_route_table_is_complete(cases: dict[str, Case]) -> None:
    """No ``org:`` scope may quietly drop out of the table above."""
    assert set(cases) == ORG_SCOPES - {"org:read"}


def test_org_read_is_enforced_by_require_scope(
    organization: Organization, oauth_app: OAuthApplication, oauth_event: t.Any
) -> None:
    """R-30: ``org:read`` has no keyed route, so ``RequireScope`` is its only enforcement."""
    case = Case("GET", ORG_READ_ROUTE, None, 200)
    owner = organization.owner
    granted = _request(owner, oauth_app, "org:read", case)
    assert granted.status_code == 200, granted.content

    for other in sorted(ORG_SCOPES - {"org:read"}):
        denied = _request(owner, oauth_app, other, case)
        assert denied.status_code == 403, (other, denied.status_code, denied.content)
        assert 'scope="org:read"' in denied["WWW-Authenticate"], (other, denied["WWW-Authenticate"])


def test_scope_without_permission_is_permission_denied(
    oauth_event: t.Any, oauth_app: OAuthApplication, oauth_staff_member: OrganizationStaff
) -> None:
    """A granted scope is not a granted permission: 403, but NOT ``insufficient_scope``.

    An app token's effective power is *granted scopes ∩ the user's org permissions*, so the
    two refusals must stay distinguishable — a client that sees ``insufficient_scope`` should
    re-consent, and one that does not should not.
    """
    oauth_staff_member.permissions = PermissionsSchema(default=PermissionMap(manage_tickets=False)).model_dump(
        mode="json"
    )
    oauth_staff_member.save(update_fields=["permissions"])
    case = Case("GET", f"/api/event-admin/{oauth_event.id}/tickets", None, 200)
    response = _request(oauth_staff_member.user, oauth_app, "org:read org:tickets", case)
    assert response.status_code == 403, response.content
    assert "insufficient_scope" not in response.get("WWW-Authenticate", "")


@pytest.mark.parametrize("path", ["members", "membership-tiers"])
def test_the_member_roster_needs_org_members_not_org_read(
    path: str, organization: Organization, oauth_app: OAuthApplication, session_client: Client
) -> None:
    """R-123: ``IsOrganizationStaff`` binds no ``PermissionKey``, so ``RequireScope`` is it.

    The roster nests every member's email, phone number, real name, pronouns and live
    subscription (plan, status, period, Stripe billing state), and the tier list sits on the
    same path as writes that already require ``org:members`` — neither is the "settings"
    ``org:read`` advertises. ``RequireScope`` constrains app tokens only, so the session
    check in the same test is what proves staff in a browser are untouched.
    """
    case = Case("GET", f"/api/organization-admin/{organization.slug}/{path}", None, 200)
    denied = _request(organization.owner, oauth_app, "org:read", case)
    assert denied.status_code == 403, denied.content
    assert 'scope="org:members"' in denied["WWW-Authenticate"], denied["WWW-Authenticate"]

    granted = _request(organization.owner, oauth_app, "org:read org:members", case)
    assert granted.status_code == 200, granted.content

    assert session_client.get(case.path).status_code == 200


def test_the_org_read_baseline_reaches_the_potluck_controller(
    oauth_event: t.Any, oauth_potluck: t.Any, organization: Organization, oauth_app: OAuthApplication
) -> None:
    """R-128: ``PotluckController`` was the one organizer surface without the baseline.

    The developer guide states ``org:read`` as a rule on every organizer route ("the scope that
    lets an app onto the organizer surface at all"), so an exception here is an exception a
    third-party developer has to discover by trial.
    """
    case = Case("GET", f"/api/events/{oauth_event.id}/potluck/", None, 200)
    denied = _request(organization.owner, oauth_app, "org:potluck", case)
    assert denied.status_code == 403, denied.content
    assert 'scope="org:read"' in denied["WWW-Authenticate"], denied["WWW-Authenticate"]
    assert _request(organization.owner, oauth_app, "org:read org:potluck", case).status_code == 200


def test_cancelling_with_refunds_needs_org_tickets_as_well(
    oauth_event: t.Any, organization: Organization, oauth_app: OAuthApplication
) -> None:
    """Cancelling is ``manage_event``/``org:events``; refunding the tickets is money.

    ``refund_tickets=true`` runs the same Stripe sweep the per-ticket refund route gates behind
    ``manage_tickets``, and ``org:tickets`` is the only scope whose label names refunds — so the
    flag needs it in addition. A plain cancel stays reachable on ``org:events`` alone.
    """
    path = f"/api/event-admin/{oauth_event.id}/actions/update-status/cancelled"
    owner = organization.owner

    denied = _request(owner, oauth_app, "org:read org:events", Case("POST", path, {"refund_tickets": True}, 200))
    assert denied.status_code == 403, denied.content
    assert 'scope="org:tickets"' in denied["WWW-Authenticate"], denied["WWW-Authenticate"]
    oauth_event.refresh_from_db()
    assert oauth_event.status != oauth_event.EventStatus.CANCELLED

    plain = _request(owner, oauth_app, "org:read org:events", Case("POST", path, {}, 200))
    assert plain.status_code == 200, plain.content
    oauth_event.refresh_from_db()
    assert oauth_event.status == oauth_event.EventStatus.CANCELLED
    assert oauth_event.tickets_refund_started_at is None

    # Re-POSTing with the flag is the documented resume path, so it is a valid second call.
    granted = _request(
        owner, oauth_app, "org:read org:events org:tickets", Case("POST", path, {"refund_tickets": True}, 200)
    )
    assert granted.status_code == 200, granted.content
    oauth_event.refresh_from_db()
    assert oauth_event.tickets_refund_started_at is not None


def test_refund_preview_needs_org_tickets_as_well(
    oauth_event: t.Any, organization: Organization, oauth_app: OAuthApplication, session_client: Client
) -> None:
    """The preview carries the connected Stripe balance and refundable revenue: ``org:tickets`` data."""
    case = Case("GET", f"/api/event-admin/{oauth_event.id}/cancellation-refund-preview", None, 200)
    owner = organization.owner
    denied = _request(owner, oauth_app, "org:read org:events", case)
    assert denied.status_code == 403, denied.content
    assert 'scope="org:tickets"' in denied["WWW-Authenticate"], denied["WWW-Authenticate"]

    assert _request(owner, oauth_app, "org:read org:events org:tickets", case).status_code == 200
    assert session_client.get(case.path).status_code == 200


def test_invitation_links_are_session_only(
    organization: Organization, oauth_app: OAuthApplication, session_client: Client
) -> None:
    """A link can grant staff status, and staff grants are session-only (R-100).

    ``org:members`` used to reach ``POST .../tokens`` with ``grants_staff_status=true``: the
    owner check lives in the service and passes for an owner-delegated token, so an app could
    mint a staff link and hand it to anyone — the end state ``POST /staff/{user_id}`` refuses.
    """
    path = f"/api/organization-admin/{organization.slug}/tokens"
    body = {"grants_membership": False, "grants_staff_status": True, "max_uses": 0}
    for method, case_body in (("POST", body), ("GET", None)):
        case = Case(method, path, case_body, 200)
        response = _request(organization.owner, oauth_app, "org:read org:members", case)
        assert response.status_code == 401, (method, response.status_code, response.content)
    assert not organization.tokens.exists()

    assert session_client.get(path).status_code == 200


def test_series_pass_admin_needs_org_tickets_as_well(
    organization: Organization, oauth_app: OAuthApplication, session_client: Client
) -> None:
    """Passes are priced ticket products, cancelled with real Stripe refunds: ``org:tickets``.

    ``edit_event_series`` maps to ``org:events``, whose label promises neither money nor
    refunds, so the controller requires ``org:tickets`` in addition — the pairing the event
    cancel-with-refunds route already uses.
    """
    series = EventSeries.objects.create(organization=organization, name="Series", slug="series")
    case = Case("GET", f"/api/event-series-admin/{series.id}/passes/", None, 200)
    owner = organization.owner
    denied = _request(owner, oauth_app, "org:read org:events", case)
    assert denied.status_code == 403, denied.content
    assert 'scope="org:tickets"' in denied["WWW-Authenticate"], denied["WWW-Authenticate"]

    assert _request(owner, oauth_app, "org:read org:events org:tickets", case).status_code == 200
    assert session_client.get(case.path).status_code == 200
