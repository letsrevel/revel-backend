"""Default-deny convention guards for the app-token surface (spec §7.3.4).

Six properties, all mechanical, none of them a list a reviewer has to trust. Properties 4-6 all
exist because of the same recurring miss: a money-or-owner route sitting inside a controller of a
different character, which per-controller reasoning cannot see. It escaped three separate careful
passes in this task, which is why it is now CI's problem rather than a reader's.

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
5. **No read scope is the sole gate on a state-changing method.** ``me:read``'s label is "See
   your profile, tickets, RSVPs and memberships", and it was authorizing a Stripe Customer
   Portal URL and a subscription checkout. A write needs a scope of its own, and until one
   exists the route stays session-only (R-99).
6. **No switched route hides an owner check in its handler body.** Properties 4 and 5 read the
   ``permissions`` list, so neither can see ``if organization.owner != self.user(): raise
   HttpError(403, ...)`` — which is how ``POST /staff/{user_id}`` stayed open while its
   ``DELETE`` sibling was pinned (R-100).
"""

import ast
import inspect
import textwrap
import typing as t

import pytest

from api.api import api
from common.authentication import ScopedJWTAuth
from events.controllers.permissions import PermissionMapPermission, RootPermission
from events.models import PermissionKey
from oauth.permissions import RequireScope
from oauth.scopes import SCOPES, UNSCOPED_KEYS, scopes_for_key

# The app-token allow-list (R-89 as corrected by R-93), enumerated LITERALLY by controller class
# name. Deriving it from ``ORGANIZATION_ADMIN_CONTROLLERS``/``EVENT_ADMIN_CONTROLLERS`` would make
# a newly added admin controller land in the allow-list automatically, and the guard would then
# fail with "expected but not switched" — nudging the next editor to *switch* it. For a
# default-deny allow-list the nudge has to point the other way: a new controller is absent here,
# so it is simply not on the surface, and adding it is a deliberate edit (M3).
APP_TOKEN_CONTROLLERS: frozenset[str] = frozenset(
    {
        # --- organization admin (13 of the 15 in ORGANIZATION_ADMIN_CONTROLLERS) ---
        # Absent on purpose: OrganizationAdminRevenueController and OrganizationAdminVATController.
        # Both are controller-level ``IsOrganizationOwner()`` and neither is honestly covered by
        # any scope in the registry: ``org:read``'s consent label is "See your organizations,
        # events and settings", which promises neither financial reporting nor changing a VAT
        # identity. A scope must never grant more than its label says (R-39), so they wait for a
        # dedicated ``org:financials`` scope rather than riding in on ``org:read`` (R-93). Note
        # that ``org:read`` still reaches *some* org settings — what it must not reach is the
        # financial identity, which is why ``GET /organization-admin/{slug}`` is pinned
        # session-only too (R-101).
        "OrganizationAdminCoreController",
        "OrganizationAdminTokensController",
        "OrganizationAdminMembershipRequestsController",
        "OrganizationAdminResourcesController",
        "OrganizationAdminMembersController",
        "OrganizationAdminVenuesController",
        "OrganizationAdminBlacklistController",
        "OrganizationAdminWhitelistController",
        "OrganizationAdminAnnouncementsController",
        "OrganizationAdminDiscountCodesController",
        "OrganizationAdminRecurringEventsController",
        "OrganizationAdminSubscriptionsController",
        "OrganizationAdminTicketsController",
        # --- event admin (all 9) ---
        "EventAdminTokensController",
        "EventAdminInvitationRequestsController",
        "EventAdminCoreController",
        "EventAdminTicketsController",
        "EventAdminInvitationsController",
        "EventAdminRSVPsController",
        "EventAdminWaitlistController",
        "EventAdminWaitlistOffersController",
        "EventAdminSeatingController",
        # --- other organizer surfaces ---
        "EventSeriesAdminController",
        "SeriesPassAdminController",
        "QuestionnaireController",
        "PollQuestionController",
        # --- attendee self-service ---
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

# A floor, not an exact count, but a TIGHT one: 259 routes are switched today, and the smallest
# switched controller has a single route, so slack here is slack in which a whole surface could
# vanish silently (M4). It exists so the parametrized guards below can never assert nothing
# (R-15); it is meant to be edited deliberately when routes are added or removed.
MINIMUM_SCOPED_ROUTES = 250

# The ``RootPermission.action`` both owner-only permission classes bind. It is not a
# ``PermissionKey``, so no scope maps to it and none ever should — "owner of the organization"
# is not something a third-party app can be granted (R-96).
OWNER_ACTION = "is_owner"

# HTTP methods that change state. A read scope must never be the sole gate on one of these: a
# consent screen that says "See your profile, tickets, RSVPs and memberships" must not also buy
# the right to start a Stripe subscription (R-99). The registry has no attendee write scope
# (``me:rsvp`` was dropped in v1 for gating nothing — R-125), so an attendee write that needs
# one stays session-only until FOLLOWUPS #5's ``me:write`` lands.
UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Scopes whose NAME promises only reading. Derived from the ``:read`` naming convention rather
# than listed, so a scope added later is covered the moment it is named — the name is the promise.
# ``test_read_only_scopes_are_the_expected_two`` pins the result so a rename cannot quietly empty
# this set and make the guard below vacuous.
READ_ONLY_SCOPES: frozenset[str] = frozenset(name for name in SCOPES if name.endswith(":read"))

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
    methods: frozenset[str]
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
                        methods=frozenset(operation.methods),
                        controller=controller,
                        auth=operation.auth_callbacks[0] if operation.auth_callbacks else None,
                        permissions=permissions,
                        handler=handler,
                    )
                )
    return routes


ROUTES = _routes()
SCOPED_ROUTES = [r for r in ROUTES if isinstance(r.auth, ScopedJWTAuth)]
UNSAFE_SCOPED_ROUTES = [r for r in SCOPED_ROUTES if r.methods & UNSAFE_METHODS]


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


def _owner_comparison_sites(func: t.Any) -> list[str]:
    """Comparisons in ``func``'s body where either side is an ``.owner``/``.owner_id`` attribute.

    This is how an owner-only rule can hide from the permission list entirely: ``add_staff`` and
    ``update_staff_permissions`` enforce owner-only with ``raise HttpError(403, ...)`` in the
    handler rather than with ``IsOrganizationOwner()`` (R-100). Source inspection is crude, but
    across the whole API it currently matches those two sites and nothing else — zero false
    positives — so it is worth a guard rather than only a comment.
    """
    try:
        source = textwrap.dedent(inspect.getsource(inspect.unwrap(func)))
    except OSError, TypeError:  # pragma: no cover - C-level or dynamically built handler
        return []
    sites: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Compare):
            continue
        for side in [node.left, *node.comparators]:
            if isinstance(side, ast.Attribute) and side.attr in ("owner", "owner_id"):
                sites.append(ast.unparse(node))
    return sites


def _effective_scope_gate(route: Route) -> tuple[frozenset[str], bool]:
    """The scopes an app token needs for ``route``, and whether it is closed to app tokens.

    Returns:
        ``(scopes, closed)``. ``closed`` is True when the route carries a keyed permission class
        whose key is in ``UNSCOPED_KEYS`` — ``scope_allows`` then raises for *every* app token, so
        the route is the safest case rather than a read-gated one. Getting this wrong is what made
        my first sweep flag ~25 ``edit_organization`` routes that are in fact unreachable.
    """
    required = {p.scope for p in route.permissions if isinstance(p, RequireScope)}
    keyed = tuple(KEYED_PERMISSION_CLASSES)
    actions = [str(p.action) for p in route.permissions if isinstance(p, keyed)]
    closed = any(action in UNSCOPED_KEYS for action in actions)
    for action in actions:
        required |= set(scopes_for_key(t.cast(PermissionKey, action)))
    return frozenset(required), closed


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


def _keyed_permission_classes() -> list[type[RootPermission]]:
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
    keyed: list[type[RootPermission]] = []
    for cls in _permission_hierarchy():
        if issubclass(cls, PermissionMapPermission):
            keyed.append(cls)
            continue
        action = _fixed_action(cls)
        if action is not None and action in keys:
            keyed.append(cls)
    return keyed


def _owner_only_permission_classes() -> list[type[RootPermission]]:
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
def test_every_keyed_permission_class_invokes_the_scope_gate(permission_class: type[RootPermission]) -> None:
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


def test_read_only_scopes_are_the_expected_two() -> None:
    """Pin the ``:read`` derivation, so the guard below cannot become vacuous by a rename."""
    assert READ_ONLY_SCOPES == {"me:read", "org:read"}, sorted(READ_ONLY_SCOPES)


def test_there_are_unsafe_scoped_routes_to_check() -> None:
    """The guard below is parametrized over a non-empty set (R-15)."""
    assert len(UNSAFE_SCOPED_ROUTES) >= 100, len(UNSAFE_SCOPED_ROUTES)


@pytest.mark.parametrize("route", UNSAFE_SCOPED_ROUTES, ids=lambda r: r.label)
def test_no_unsafe_route_is_gated_only_by_a_read_scope(route: Route) -> None:
    """A read scope must never be the sole gate on a state-changing method (R-99).

    A route that changes state must require either a write scope, or an ``UNSCOPED_KEYS`` key
    (which refuses every app token), or nothing at all because it is session-only. The registry
    has no attendee write scope at all in v1 (R-125), so every attendee write is in the third
    category.

    This is the mechanical form of the miss that recurred three times in this task: a
    money-or-owner route sitting inside a controller of a different character, invisible to
    per-controller reasoning. ``POST /api/me/organizations/{org_id}/billing-portal`` returned a
    Stripe Customer Portal URL under a scope labelled "See your profile, tickets, RSVPs and
    memberships".
    """
    scopes, closed = _effective_scope_gate(route)
    if closed:
        return
    assert not (scopes and scopes <= READ_ONLY_SCOPES), (
        f"{route.label} changes state but its only scope gate is {sorted(scopes)}, whose label "
        f"promises only reading. Give it a write scope, or keep the route on auth=I18nJWTAuth()."
    )


@pytest.mark.parametrize("route", SCOPED_ROUTES, ids=lambda r: r.label)
def test_no_switched_route_hides_an_owner_check_in_its_handler(route: Route) -> None:
    """An owner-only rule enforced in the handler body is invisible to the permission list (R-100).

    ``test_no_owner_only_route_accepts_an_app_token`` inspects ``permissions``, so it cannot see
    ``if organization.owner != self.user(): raise HttpError(403, ...)``. That is exactly how
    ``POST /staff/{user_id}`` stayed reachable on ``org:read org:members`` while its ``DELETE``
    sibling was correctly pinned session-only.

    If a route legitimately needs a body-level owner comparison *and* app-token access, this
    assertion is where that has to be argued — not in the handler.
    """
    sites = _owner_comparison_sites(route.handler)
    assert not sites, (
        f"{route.label} accepts app tokens and compares an owner attribute in its handler body "
        f"({sites}); the permission-list guards cannot see that. Pin the route session-only with "
        f"auth=I18nJWTAuth(), or express the rule with IsOrganizationOwner()."
    )


class _DetectorFixture:
    """A stand-in controller for the AST detectors, with object resolution at known depths.

    The detectors are unit-tested against this rather than only through property 2 (R-106,
    R-126): property 2 early-returns for every route carrying a ``RequireScope``, and all of
    them do today, so ``_triggers_object_check``, ``_self_call_names`` and
    ``_OBJECT_CHECK_CALLS`` were ~40 lines that no test ever called. If the detector silently
    started returning True unconditionally, property 2 would keep passing and the R-50
    protection would be gone. The machinery is kept rather than deleted because a future
    keyed-permission-only route makes property 2's second half load-bearing again.
    """

    def resolves_directly(self) -> None:
        """Hop 0: the handler itself triggers the hook."""
        self.get_object_or_exception(object())

    def resolves_nothing(self) -> None:
        """No hop reaches the hook, so this handler needs an explicit ``RequireScope``."""
        self.plain_helper()

    def resolves_via_one_helper(self) -> None:
        """Hop 1."""
        self.level_1()

    def level_1(self) -> None:
        """Resolve the object one hop from the handler."""
        self.get_object_or_exception(object())

    def resolves_via_two_helpers(self) -> None:
        """Hop 2 — the ``get_one`` → ``get_object_or_exception`` shape the real controllers use."""
        self.level_2a()

    def level_2a(self) -> None:
        """Delegate one hop further."""
        self.level_2b()

    def level_2b(self) -> None:
        """Resolve the object two hops from the handler."""
        self.get_object_or_exception(object())

    def resolves_too_deep(self) -> None:
        """Hop 4 — past the ``depth=3`` budget, so the detector must fail closed."""
        self.deep_1()

    def deep_1(self) -> None:
        """Hop 1 of 4."""
        self.deep_2()

    def deep_2(self) -> None:
        """Hop 2 of 4."""
        self.deep_3()

    def deep_3(self) -> None:
        """Hop 3 of 4."""
        self.deep_4()

    def deep_4(self) -> None:
        """Hop 4 of 4: reachable in principle, out of budget in practice."""
        self.get_object_or_exception(object())

    def plain_helper(self) -> None:
        """A helper that resolves nothing."""

    def get_object_or_exception(self, obj: object) -> None:
        """Stand in for ninja-extra's object hook; only its *name* matters to the detector."""

    def compares_an_owner(self) -> None:
        """The handler-body owner check property 6 hunts for."""
        organization = object()
        if organization.owner != self:  # type: ignore[attr-defined]
            raise PermissionError

    def compares_no_owner(self) -> None:
        """A comparison whose attribute operand is not ``owner``, so property 6 must not match."""
        event = object()
        if event.organizer != self:  # type: ignore[attr-defined]
            raise PermissionError


def test_self_call_names_reads_only_calls_on_self() -> None:
    """The walk finds ``self.<name>(...)`` and nothing else (R-126)."""
    assert _self_call_names(_DetectorFixture.resolves_via_two_helpers) == {"level_2a"}
    assert _self_call_names(_DetectorFixture.plain_helper) == frozenset()


@pytest.mark.parametrize(
    ("handler_name", "expected"),
    [
        ("resolves_directly", True),
        ("resolves_via_one_helper", True),
        ("resolves_via_two_helpers", True),
        ("resolves_nothing", False),
        ("resolves_too_deep", False),
    ],
)
def test_triggers_object_check_finds_resolution_within_its_depth_budget(handler_name: str, expected: bool) -> None:
    """Property 2's second half, exercised directly — including that it fails closed (R-126).

    ``resolves_too_deep`` is the fail-closed case: the object *is* resolved, four hops away,
    and the detector still answers False, so such a route would be required to carry an
    explicit ``RequireScope`` rather than be trusted to the hook.
    """
    handler = getattr(_DetectorFixture, handler_name)
    assert _triggers_object_check(_DetectorFixture, handler) is expected


def test_triggers_object_check_fails_closed_without_a_controller() -> None:
    """No controller means no helper to follow, so only the handler's own body can say True."""
    assert _triggers_object_check(None, _DetectorFixture.resolves_directly) is True
    assert _triggers_object_check(None, _DetectorFixture.resolves_via_one_helper) is False


def test_owner_comparison_sites_matches_the_real_handler_it_was_written_for() -> None:
    """Property 6's teeth, made permanent rather than historical (R-106).

    ``add_staff`` is pinned session-only, so it has left ``SCOPED_ROUTES`` and property 6
    matches nothing in CI. Asserting the detector against the handler it was written for is
    what keeps a silently broken detector from passing.

    The false-negative surface is narrow and deliberate: the pass does not recurse into
    helpers and matches only when an ``ast.Compare`` operand is directly an attribute named
    ``owner``/``owner_id``. It misses ``org.owner.pk != user.pk``, a local alias, a membership
    test, a helper predicate like ``org.is_owner(user)`` and any service-level assertion. Zero
    false positives is not zero false negatives — this is a tripwire for one idiom.
    """
    from events.controllers.organization_admin.members import OrganizationAdminMembersController

    sites = _owner_comparison_sites(OrganizationAdminMembersController.add_staff)
    assert sites == ["organization.owner != self.user()"], sites
    assert _owner_comparison_sites(_DetectorFixture.compares_an_owner) == ["organization.owner != self"]
    assert _owner_comparison_sites(_DetectorFixture.compares_no_owner) == []
