"""Default-deny convention guards for the app-token surface (spec §7.3.4).

Four properties, all mechanical, none of them a list a reviewer has to trust:

1. **The surface is exactly the reviewed set.** The controllers that accept app tokens ARE
   the attack surface (R-89), so widening it must be an explicit edit to
   :data:`APP_TOKEN_CONTROLLERS` — which is what makes the widening reviewable.
2. **Every route on that surface is scope-enforced, not merely scope-declared.**
   Declaring a keyed permission class is not enforcement: the ``scope_allows`` gate lives
   only in ``has_object_permission``, and ``RootPermission.has_permission`` returns True
   unconditionally, so a route that never triggers ninja-extra's
   ``check_object_permissions`` would reach its handler with no scope check at all (R-50).
3. **Every keyed permission class invokes the gate.** Discovered by walking the class
   hierarchy at runtime rather than by listing seven known names, so an eighth subclass
   added later cannot silently skip enforcement (R-49).
4. **No owner-only route accepts an app token.** Property 1 works at controller granularity,
   so it cannot see a wrongly-scoped route inside a correctly-switched controller — which is
   how ``POST stripe/connect``, ``POST stripe/account/verify`` and ``DELETE staff/{user_id}``
   stayed reachable on ``org:read`` through two careful passes. This guard is per route (R-96).
"""

import ast
import inspect
import textwrap
import typing as t

import pytest
from ninja_extra.permissions import BasePermission

from api.api import api
from common.authentication import ScopedJWTAuth
from events.controllers.event_admin import EVENT_ADMIN_CONTROLLERS
from events.controllers.organization_admin import ORGANIZATION_ADMIN_CONTROLLERS
from events.controllers.permissions import PermissionMapPermission, RootPermission
from events.models import PermissionKey
from oauth.permissions import RequireScope

# Org-admin controllers deliberately held back from the app-token surface (R-93). Both are
# controller-level ``IsOrganizationOwner()`` and neither is honestly covered by any scope in the
# registry: ``org:read``'s consent label is "See your organizations, events and settings", which
# promises neither financial reporting nor changing a VAT identity. A scope must never grant more
# than its label says (R-39), so these wait for a dedicated ``org:financials`` scope rather than
# riding in on ``org:read``.
SESSION_ONLY_ORGANIZATION_ADMIN: frozenset[str] = frozenset(
    {"OrganizationAdminRevenueController", "OrganizationAdminVATController"}
)

# The app-token allow-list (R-89 as corrected by R-93), by controller class name. Everything else
# stays on ``I18nJWTAuth``/``OptionalAuth`` and refuses app tokens by construction.
APP_TOKEN_CONTROLLERS: frozenset[str] = frozenset(
    ({c.__name__ for c in ORGANIZATION_ADMIN_CONTROLLERS} - SESSION_ONLY_ORGANIZATION_ADMIN)
    | {c.__name__ for c in EVENT_ADMIN_CONTROLLERS}
    | {
        # Organizer surfaces.
        "EventSeriesAdminController",
        "SeriesPassAdminController",
        "QuestionnaireController",
        "PollQuestionController",
        # Attendee self-service.
        "DashboardController",
        "MeSubscriptionsController",
        "MeMembershipApplicationsController",
        "MeMembershipQuestionnaireController",
        "FollowingController",
        "PotluckController",
        # ``GET /api/account/me`` only; the rest of the controller stays session-only.
        "AccountController",
    }
)

# A floor, not an exact count: it fails loudly if a whole surface is un-switched by a later
# refactor, rather than silently asserting nothing (R-15). 273 routes are switched today.
MINIMUM_SCOPED_ROUTES = 230

# The ``RootPermission.action`` both owner-only permission classes bind. It is not a
# ``PermissionKey``, so no scope maps to it and none ever should — "owner of the organization"
# is not something a third-party app can be granted (R-96).
OWNER_ACTION = "is_owner"

# Calls on ``self`` that run ninja-extra's object-level permission hook.
_OBJECT_CHECK_CALLS: frozenset[str] = frozenset(
    {
        "get_object_or_exception",
        "aget_object_or_exception",
        "get_object_or_none",
        "aget_object_or_none",
        "check_object_permissions",
        "async_check_object_permissions",
    }
)


class Route(t.NamedTuple):
    """One registered operation, flattened for assertion."""

    label: str
    controller: type | None
    auth: t.Any
    permissions: list[t.Any]
    handler: t.Any


def _routes() -> list[Route]:
    """Every operation registered on the API, with its *effective* permission list.

    ninja-extra resolves permissions as ``route.permissions or controller.permission_classes``
    (``controllers/route/route_functions.py``), i.e. a route-level list REPLACES the
    controller-level one rather than extending it — which is the whole reason R-07 requires
    ``RequireScope`` on the route-level lists too.
    """
    routes: list[Route] = []
    for prefix, router in api._routers:  # noqa: SLF001
        controller = getattr(router, "controller_class", None)
        for path, path_view in router.path_operations.items():
            for operation in path_view.operations:
                # A handful of routes are registered on a plain ninja ``Router`` rather than a
                # controller; they have no permission layer at all and can never be switched.
                get_route_function = getattr(operation.view_func, "get_route_function", None)
                permissions: list[t.Any] = []
                handler: t.Any = operation.view_func
                if get_route_function is not None:
                    route_function = get_route_function()
                    route = route_function.route
                    permissions = list(route.permissions or route_function.api_controller.permission_classes or [])
                    handler = route.view_func
                routes.append(
                    Route(
                        label=f"{','.join(operation.methods)} {prefix}{path}",
                        controller=controller,
                        auth=operation.auth_callbacks[0] if operation.auth_callbacks else None,
                        permissions=permissions,
                        handler=handler,
                    )
                )
    return routes


ROUTES = _routes()
SCOPED_ROUTES = [r for r in ROUTES if isinstance(r.auth, ScopedJWTAuth)]


def _self_call_names(func: t.Any) -> frozenset[str]:
    """Names of every ``self.<name>(...)`` call in ``func``'s body."""
    try:
        source = textwrap.dedent(inspect.getsource(inspect.unwrap(func)))
    except OSError, TypeError:  # pragma: no cover - C-level or dynamically built handler
        return frozenset()
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        ):
            names.add(node.func.attr)
    return frozenset(names)


def _triggers_object_check(controller: type | None, func: t.Any, depth: int = 3) -> bool:
    """Whether ``func`` reaches ``check_object_permissions``, directly or via a helper.

    Fails CLOSED: an unreadable or too-deeply-nested handler counts as *not* triggering
    the hook, so it must carry an explicit ``RequireScope`` to pass the guard below.
    """
    calls = _self_call_names(func)
    if calls & _OBJECT_CHECK_CALLS:
        return True
    if controller is None or depth <= 0:
        return False
    return any(
        _triggers_object_check(controller, helper, depth - 1)
        for name in calls
        if callable(helper := getattr(controller, name, None))
    )


def _permission_hierarchy() -> list[type[RootPermission]]:
    """Every concrete ``RootPermission`` subclass, found recursively at runtime.

    Discovery, not a list: ``polls.permissions`` lives outside ``events`` and a future app
    could add another, so the walk is what keeps the guards below honest (R-49). "Concrete"
    means the class defines its own ``has_object_permission`` — the abstract middle of the
    hierarchy (``PermissionMapPermission``) contributes no route decisions.
    """
    # Importing the permission modules is what populates ``__subclasses__``; the controllers
    # are imported by ``api.api`` above, which pulls both in, but be explicit about it.
    import polls.permissions  # noqa: F401  # registers PollPermission / IsPollOrganizationOwner

    def descendants(base: type[RootPermission]) -> set[type[RootPermission]]:
        found: set[type[RootPermission]] = set()
        for sub in base.__subclasses__():
            found.add(sub)
            found |= descendants(sub)
        return found

    classes = (cls for cls in descendants(RootPermission) if "has_object_permission" in cls.__dict__)
    return sorted(classes, key=lambda c: c.__name__)


def _fixed_action(permission_class: type[RootPermission]) -> str | None:
    """The ``action`` a zero-argument permission class binds, or None if it takes one.

    ``RootPermission.__init__`` requires an action; the fixed-action subclasses override it to
    take none, and only those have an action knowable without a call site.
    """
    try:
        return str(permission_class().action)  # type: ignore[call-arg]
    except TypeError:
        return None


def _keyed_permission_classes() -> list[type[BasePermission]]:
    """Every permission class whose ``action`` is a ``PermissionKey``, found by introspection.

    Two families:

    * every concrete ``PermissionMapPermission`` subclass — its ``action`` is a
      ``PermissionKey`` by construction; and
    * every other concrete ``RootPermission`` subclass that takes no constructor argument and
      whose fixed ``action`` is a ``PermissionKey`` (``CanDuplicateEvent``,
      ``ManagePotluckPermission``).

    Deliberately excluded: ``IsOrganizationOwner``/``IsPollOrganizationOwner``/
    ``IsOrganizationStaff``/``CanPurchaseTicket`` (actions ``is_owner``/``is_staff``/
    ``can_purchase`` are not ``PermissionMap`` keys) and ``PotluckItemPermission`` (an
    action-taking ``RootPermission`` whose actions are not keys either) — see R-46.
    """
    keys = set(t.get_args(PermissionKey))
    keyed: list[type[BasePermission]] = []
    for cls in _permission_hierarchy():
        if issubclass(cls, PermissionMapPermission):
            keyed.append(cls)
            continue
        action = _fixed_action(cls)
        if action is not None and action in keys:
            keyed.append(cls)
    return keyed


def _owner_only_permission_classes() -> list[type[BasePermission]]:
    """Permission classes that restrict a route to the organization *owner*.

    Discovered by the action they bind — ``is_owner`` — rather than by name, so that
    ``polls.permissions.IsPollOrganizationOwner`` is found alongside
    ``events.controllers.permissions.IsOrganizationOwner`` and a third one could not appear
    unnoticed. ``IsOrganizationStaff`` (``is_staff``) is deliberately NOT in this category:
    staff-gated reads are legitimately app-token-reachable on ``org:read``.
    """
    return [cls for cls in _permission_hierarchy() if _fixed_action(cls) == OWNER_ACTION]


KEYED_PERMISSION_CLASSES = _keyed_permission_classes()
OWNER_ONLY_PERMISSION_CLASSES = _owner_only_permission_classes()


def test_the_app_token_surface_is_exactly_the_reviewed_set() -> None:
    """Only the controllers R-89 justified may accept app tokens."""
    switched = {r.controller.__name__ for r in SCOPED_ROUTES if r.controller is not None}
    assert switched == APP_TOKEN_CONTROLLERS, {
        "unexpectedly switched": sorted(switched - APP_TOKEN_CONTROLLERS),
        "expected but not switched": sorted(APP_TOKEN_CONTROLLERS - switched),
    }


def test_the_switched_surface_is_not_empty() -> None:
    """A guard that skips every case asserts nothing (R-15)."""
    assert len(SCOPED_ROUTES) >= MINIMUM_SCOPED_ROUTES, len(SCOPED_ROUTES)


@pytest.mark.parametrize("route", SCOPED_ROUTES, ids=lambda r: r.label)
def test_every_scoped_route_enforces_a_scope(route: Route) -> None:
    """Every app-token-reachable route runs a scope check before its handler.

    Either ``RequireScope.has_permission`` (which ninja-extra always calls) or a keyed
    permission class whose ``has_object_permission`` the handler actually triggers.
    """
    explicit = [p for p in route.permissions if isinstance(p, RequireScope)]
    if explicit:
        return
    keyed = [p for p in route.permissions if isinstance(p, tuple(KEYED_PERMISSION_CLASSES))]
    assert keyed, (
        f"{route.label} accepts app tokens but declares no scope source; "
        f"prepend RequireScope(...) or keep the route on auth=I18nJWTAuth()"
    )
    assert _triggers_object_check(route.controller, route.handler), (
        f"{route.label} declares {[type(p).__name__ for p in keyed]} but never resolves an "
        f"object through the controller, so has_object_permission — where the scope gate "
        f"lives — never runs. Prepend RequireScope(...)."
    )


def test_keyed_permission_classes_are_discovered_by_introspection() -> None:
    """The hierarchy walk finds at least the seven gate sites R-46 enumerated."""
    found = {cls.__name__ for cls in KEYED_PERMISSION_CLASSES}
    assert found >= {
        "CanDuplicateEvent",
        "EventPermission",
        "EventSeriesPermission",
        "ManagePotluckPermission",
        "OrganizationPermission",
        "PollPermission",
        "QuestionnairePermission",
    }, sorted(found)
    assert "PotluckItemPermission" not in found
    assert "IsOrganizationStaff" not in found


@pytest.mark.parametrize("permission_class", KEYED_PERMISSION_CLASSES, ids=lambda c: c.__name__)
def test_every_keyed_permission_class_invokes_the_scope_gate(permission_class: type[BasePermission]) -> None:
    """``has_object_permission`` must call ``scope_allows`` — R-49, checked for real subclasses.

    Source inspection rather than a call, because the seven classes take different object
    types; ``test_permissions.py`` proves the behaviour, this proves the coverage.
    """
    source = inspect.getsource(permission_class.has_object_permission)
    assert "scope_allows(" in source, f"{permission_class.__name__}.has_object_permission skips the scope gate"


def test_require_scope_rejects_an_unknown_scope() -> None:
    """R-51: a typo must fail at import time, not silently deny every app token.

    ``RequireScope`` compares with a plain ``not in``, so ``RequireScope("org:tikets")``
    would refuse every token on that route with no signal anywhere.
    """
    with pytest.raises(ValueError, match="Unknown scope"):
        RequireScope("org:tikets")


def test_owner_only_classes_are_discovered_by_introspection() -> None:
    """The ``is_owner`` walk finds both owner-only classes, and nothing that is merely staff.

    ``IsPollOrganizationOwner`` lives in ``polls``, so a guard keyed on class names declared in
    ``events`` would have missed it — which is the whole reason this is a hierarchy walk.
    """
    found = {cls.__name__ for cls in OWNER_ONLY_PERMISSION_CLASSES}
    assert found == {"IsOrganizationOwner", "IsPollOrganizationOwner"}, sorted(found)
    assert "IsOrganizationStaff" not in found


@pytest.mark.parametrize("route", SCOPED_ROUTES, ids=lambda r: r.label)
def test_no_owner_only_route_accepts_an_app_token(route: Route) -> None:
    """An owner-only route must never be reachable with a third-party app token (R-96).

    "Owner of the organization" is a relationship, not a delegable capability: the owner-only
    routes are Stripe Connect onboarding, VAT identity, revenue reporting and staff removal, and
    no scope label in the registry honestly promises any of them. A scope must never grant more
    than its label says (R-39), so these routes stay on ``auth=I18nJWTAuth()``.

    If a future scope (e.g. ``org:financials``) is meant to reach one of them, this test is where
    that decision gets recorded — edit it deliberately rather than letting a route drift in.
    """
    owner_only = [p for p in route.permissions if isinstance(p, tuple(OWNER_ONLY_PERMISSION_CLASSES))]
    assert not owner_only, (
        f"{route.label} is gated by {[type(p).__name__ for p in owner_only]} but accepts app "
        f"tokens. Owner-only routes are session-only: put auth=I18nJWTAuth() on the route "
        f"(or the controller) and drop any RequireScope from its permission list."
    )
