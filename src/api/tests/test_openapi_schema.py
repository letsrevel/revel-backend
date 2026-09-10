"""OpenAPI component-name collision checks (#782).

django-ninja merges each operation's ``$defs`` into ``components.schemas``
with a bare ``dict.update`` — last writer wins. Two classes sharing a bare
name (subscription ``PaymentMethod`` vs ``TicketTier.PaymentMethod``,
membership ``ReasonCode`` vs event ``ReasonCode``) therefore silently
clobbered each other, mis-narrowing every generated client. These tests pin
the fix (distinct class names) and the guard that keeps it from recurring.
"""

import types
import typing as t

import pytest
from ninja.openapi.schema import OpenAPISchema

from api.api import api
from api.management.commands.dump_openapi import OpenAPINameCollisionError, schema_name_collision_guard

pytestmark = pytest.mark.django_db


def _components() -> dict[str, t.Any]:
    # Drop the process-level memo (#880) so the guard actually observes generation.
    api._schema_cache = {}
    with schema_name_collision_guard():
        schema = api.get_openapi_schema()
    return t.cast(dict[str, t.Any], schema["components"]["schemas"])


def test_openapi_schema_is_memoized() -> None:
    """Repeat calls return the same object — the schema is built once per process (#880)."""
    api._schema_cache = {}
    first = api.get_openapi_schema()
    second = api.get_openapi_schema()
    assert first is second


def test_schema_generation_has_no_component_name_collisions() -> None:
    """Generating the full spec under the guard must not raise."""
    assert _components()


def test_colliding_enums_have_distinct_names_and_full_value_sets() -> None:
    """The four #782 enums coexist under distinct names with their full values."""
    components = _components()

    ticket_pm = components["PaymentMethod"]["enum"]
    assert set(ticket_pm) == {"online", "offline", "at_the_door", "free"}

    sub_pm = components["SubscriptionPaymentMethod"]["enum"]
    # "free" (#832) is the Stripe-less, member-self-serve plan; it deliberately
    # collides with TicketTier.PaymentMethod.FREE above, which is the whole
    # point of keeping the two enums under distinct component names.
    assert set(sub_pm) == {"online", "offline", "free"}

    event_rc = components["ReasonCode"]["enum"]
    membership_rc = components["MembershipReasonCode"]["enum"]
    # Marker values unique to each domain prove neither clobbered the other.
    assert "tier_requires_subscription" in membership_rc
    assert "tier_requires_subscription" not in event_rc
    assert len(event_rc) > len(membership_rc)

    # Two more collisions the guard surfaced beyond the ones reported in #782:
    # the membership-application state machine vs the 3-value
    # UserRequestMixin.Status, and the two invoice status enums.
    assert set(components["Status"]["enum"]) == {"pending", "approved", "rejected"}
    assert set(components["MembershipRequestStatus"]["enum"]) == {
        "pending",
        "approved",
        "rejected",
        "cancelled",
        "completed",
    }
    assert set(components["InvoiceStatus"]["enum"]) == {"draft", "issued", "paid", "cancelled"}
    assert set(components["AttendeeInvoiceStatus"]["enum"]) == {"draft", "issued", "cancelled"}


def test_guard_raises_on_conflicting_redefinition() -> None:
    """Same name + different definition must raise; identical re-adds must not."""
    # The patched method only touches ``self.schemas``, so a duck-typed stand-in
    # avoids constructing a real OpenAPISchema (whose __init__ needs a NinjaAPI).
    fake = t.cast(
        OpenAPISchema,
        types.SimpleNamespace(schemas={"Clash": {"enum": ["a", "b"], "title": "Clash", "type": "string"}}),
    )
    with schema_name_collision_guard():
        # Identical re-definition: allowed (common for equal-valued enums).
        OpenAPISchema.add_schema_definitions(fake, {"Clash": {"enum": ["a", "b"], "title": "Clash", "type": "string"}})
        with pytest.raises(OpenAPINameCollisionError):
            OpenAPISchema.add_schema_definitions(
                fake, {"Clash": {"enum": ["a", "b", "c"], "title": "Clash", "type": "string"}}
            )


# --- 400 response-body contract (#712) -------------------------------------
#
# 400 bodies come from exception handlers that return a raw ``Response``,
# bypassing Ninja's response serialization entirely — nothing validates them
# against the declared schema, so declaration and reality drift silently. These
# invariants pin the declarations; the wire shapes themselves are proven in
# ``events/tests/test_controllers/test_error_response_contracts.py``.

#: The only two operations that genuinely ``return 400, ResponseMessage(...)``.
RESPONSE_MESSAGE_400_ALLOWLIST = {
    "/api/events/claim-invitation/{token}",
    "/api/organizations/claim-invitation/{token}",
}

#: Every ``(path, status)`` at which a view genuinely returns ``ResponseMessage`` as
#: an *error* body. Anything else declaring it at >= 400 is a mis-declaration (#826).
RESPONSE_MESSAGE_ERROR_ALLOWLIST = {(path, "400") for path in RESPONSE_MESSAGE_400_ALLOWLIST} | {
    ("/api/events/tokens/{token_id}", "404"),
    ("/api/organizations/tokens/{token_id}", "404"),
}

#: Every component a 400 is allowed to resolve to.
KNOWN_400_COMPONENTS = {
    "ErrorDetail",
    "EventUserEligibility",
    "GuestActionErrorSchema",
    "MembershipEligibilitySchema",
    "ResponseMessage",
    "ValidationErrorResponse",
}


def _declared_error_schemas(status_filter: t.Callable[[str], bool]) -> dict[tuple[str, str, str], set[str]]:
    """Map each ``(path, method, status)`` matching ``status_filter`` to its component names."""
    # Drop the process-level memo (#880) so the guard actually observes generation.
    api._schema_cache = {}
    with schema_name_collision_guard():
        spec = api.get_openapi_schema()

    declared: dict[tuple[str, str, str], set[str]] = {}
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            if not isinstance(operation, dict):
                continue
            for status, response in operation.get("responses", {}).items():
                if not status_filter(str(status)):
                    continue
                body = response.get("content", {}).get("application/json", {}).get("schema", {})
                refs = body.get("anyOf", [body])
                declared[(path, method, str(status))] = {
                    ref["$ref"].rsplit("/", 1)[-1] for ref in refs if isinstance(ref, dict) and "$ref" in ref
                }
    return declared


def _declared_400_schemas() -> dict[tuple[str, str], set[str]]:
    """Map each ``(path, method)`` that declares a 400 to its component names."""
    return {
        (path, method): names
        for (path, method, _status), names in _declared_error_schemas(lambda s: s == "400").items()
    }


def test_response_message_never_declared_on_error_responses_outside_allowlist() -> None:
    """Every error status repo-wide emits ``{detail}`` unless a view really returns ``{message}``.

    #712 fixed the 400s and the two subscription controllers; #826 widens the rule
    to every operation, since 403/404/422/502 were declared as ``ResponseMessage``
    on routes whose only producers are ``HttpError``/``get_object_or_404``.
    """
    declared = _declared_error_schemas(lambda s: s.isdigit() and int(s) >= 400)
    assert len(declared) > 400, f"expected the whole API's error responses, matched {len(declared)}"

    offenders = sorted(
        f"{method.upper()} {path} -> {status}"
        for (path, method, status), names in declared.items()
        if "ResponseMessage" in names and (path, status) not in RESPONSE_MESSAGE_ERROR_ALLOWLIST
    )
    assert not offenders, f"ResponseMessage declared on error responses that never return it: {offenders}"


def test_response_message_declared_at_400_only_where_it_is_returned() -> None:
    """``{"message": ...}`` at 400 must only be declared where a view returns it.

    Before #712, 26 endpoints declared ``ResponseMessage`` for 400 while no
    reachable path produced a ``message`` key, so the generated client typed
    every one of those bodies wrongly.
    """
    offenders = sorted(
        path
        for (path, _method), names in _declared_400_schemas().items()
        if "ResponseMessage" in names and path not in RESPONSE_MESSAGE_400_ALLOWLIST
    )
    assert not offenders, f"ResponseMessage declared at 400 on endpoints that never return it: {offenders}"


def test_every_declared_400_resolves_to_a_known_component() -> None:
    """No 400 may resolve to an inline/anonymous schema the generated client cannot name."""
    # An inline/anonymous schema resolves to *no* component name at all, so the
    # empty set must count as a failure — otherwise this guard silently skips the
    # exact case it exists to catch.
    unknown = {
        key: names - KNOWN_400_COMPONENTS
        for key, names in _declared_400_schemas().items()
        if not names or names - KNOWN_400_COMPONENTS
    }
    assert not unknown, f"Unexpected 400 response schemas: {unknown}"


def test_eligibility_endpoints_also_declare_the_detail_shape() -> None:
    """RSVP/checkout reject with a plain ``HttpError`` as well as an eligibility payload."""
    declared = _declared_400_schemas()
    for path in (
        "/api/events/{event_id}/rsvp/{answer}",
        "/api/events/{event_id}/tickets/{tier_id}/checkout",
        "/api/events/{event_id}/tickets/{tier_id}/checkout/pwyc",
    ):
        assert declared[(path, "post")] == {"EventUserEligibility", "ErrorDetail"}, path


def test_guest_endpoints_declare_the_coded_error_shape() -> None:
    """Guest RSVP/checkout can also refuse with a ``code``-bearing body (#905).

    ``guest_account_exists`` is reachable from every endpoint that mints a guest
    user; ``guest_cart_too_large`` from the non-online checkout branches. One
    schema covers both so the generated client narrows ``code`` to the enum.
    """
    declared = _declared_400_schemas()
    for path in (
        "/api/events/{event_id}/rsvp/{answer}/public",
        "/api/events/{event_id}/tickets/{tier_id}/checkout/public",
        "/api/events/{event_id}/tickets/{tier_id}/checkout/pwyc/public",
        "/api/events/{event_id}/checkout/public",
    ):
        assert declared[(path, "post")] == {"EventUserEligibility", "ErrorDetail", "GuestActionErrorSchema"}, path


def _operations() -> dict[tuple[str, str], dict[str, t.Any]]:
    """Map each ``(path, method)`` to its raw OpenAPI operation object."""
    api._schema_cache = {}
    with schema_name_collision_guard():
        spec = api.get_openapi_schema()
    return {
        (path, method): operation
        for path, operations in spec["paths"].items()
        for method, operation in operations.items()
        if isinstance(operation, dict)
    }


def test_secured_operations_declare_401_and_403_as_error_detail() -> None:
    """Every operation behind an auth class can fail auth (401) or a permission check (403).

    ``RootPermission.has_permission`` is unconditionally ``True`` and the real check is
    object-level inside ``get_one()``, so 403 is reachable on effectively every
    permissioned route while only a handful declared it (#826). Declared globally.
    """
    declared = _declared_error_schemas(lambda s: s in {"401", "403"})
    secured = [key for key, op in _operations().items() if "security" in op]
    assert len(secured) > 400, f"expected most operations to be secured, matched {len(secured)}"

    missing = sorted(
        f"{method.upper()} {path} -> {status}"
        for (path, method) in secured
        for status in ("401", "403")
        if declared.get((path, method, status)) != {"ErrorDetail"}
    )
    assert not missing, f"secured operations without an ErrorDetail 401/403: {missing}"


def test_parameterised_operations_declare_the_request_validation_422() -> None:
    """Every operation that parses a path/query/body param can answer ninja's 422.

    Its body is ``{"detail": [ {type, loc, msg, ctx?}, ... ]}`` — a *list*, so it must
    resolve to ``RequestValidationError`` rather than ``ErrorDetail`` (#826).
    """
    declared = _declared_error_schemas(lambda s: s == "422")
    parameterised = [key for key, op in _operations().items() if op.get("parameters") or op.get("requestBody")]
    assert len(parameterised) > 400, f"expected most operations to take params, matched {len(parameterised)}"

    missing = sorted(
        f"{method.upper()} {path}"
        for (path, method) in parameterised
        if "RequestValidationError" not in declared.get((path, method, "422"), set())
    )
    assert not missing, f"parameterised operations without the RequestValidationError 422: {missing}"


def test_domain_422s_keep_their_detail_shape_alongside_the_validation_one() -> None:
    """A route with its own ``HttpError(422)``/static-handler 422 declares both shapes."""
    declared = _declared_error_schemas(lambda s: s == "422")
    for path, method in (
        ("/api/organization-admin/{slug}/tiers/{tier_id}/plans", "post"),
        ("/api/organization-admin/{slug}/plans/{plan_id}", "patch"),
        # ``BillingInfoRequiredError`` renders ``{detail}`` via a static handler; these
        # two declared the ``{errors}`` shape that nothing at 422 produces.
        ("/api/event-admin/{event_id}/ticket-tier", "post"),
        ("/api/event-admin/{event_id}/ticket-tier/{tier_id}", "put"),
    ):
        assert declared[(path, method, "422")] == {"ErrorDetail", "RequestValidationError"}, path
