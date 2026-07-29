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
    with schema_name_collision_guard():
        schema = api.get_openapi_schema()
    return t.cast(dict[str, t.Any], schema["components"]["schemas"])


def test_schema_generation_has_no_component_name_collisions() -> None:
    """Generating the full spec under the guard must not raise."""
    assert _components()


def test_colliding_enums_have_distinct_names_and_full_value_sets() -> None:
    """The four #782 enums coexist under distinct names with their full values."""
    components = _components()

    ticket_pm = components["PaymentMethod"]["enum"]
    assert set(ticket_pm) == {"online", "offline", "at_the_door", "free"}

    sub_pm = components["SubscriptionPaymentMethod"]["enum"]
    assert set(sub_pm) == {"online", "offline"}

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

#: Every component a 400 is allowed to resolve to.
KNOWN_400_COMPONENTS = {
    "ErrorDetail",
    "EventUserEligibility",
    "MembershipEligibilitySchema",
    "ResponseMessage",
    "ValidationErrorResponse",
}


#: Path prefixes of the two subscription controllers, whose every error status was
#: audited in #712. ``ResponseMessage`` must never reappear on them.
SUBSCRIPTION_PATH_MARKERS = (
    "/api/me/organizations/{org_id}/sub",
    "/api/me/organizations/{org_id}/billing-portal",
    "/api/organization-admin/{slug}/plans",
    "/api/organization-admin/{slug}/subscriptions",
    "/api/organization-admin/{slug}/tiers/{tier_id}/plans",
    "/api/organization-admin/{slug}/payments",
)


def _declared_error_schemas(status_filter: t.Callable[[str], bool]) -> dict[tuple[str, str, str], set[str]]:
    """Map each ``(path, method, status)`` matching ``status_filter`` to its component names."""
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


def test_subscription_controllers_never_declare_response_message() -> None:
    """Every error status on the two subscription controllers emits ``{detail}``.

    #712 fixed the 400s; the same mis-declaration covered 403/404/422/502 on
    these two controllers (21 sites), where no view returns a ``message`` key
    at any status.
    """
    declared = _declared_error_schemas(lambda s: s.isdigit() and int(s) >= 400)
    covered = [key for key in declared if any(key[0].startswith(marker) for marker in SUBSCRIPTION_PATH_MARKERS)]
    # Guard against the markers silently drifting off the real paths, which would
    # make this test pass vacuously.
    assert len(covered) > 40, f"expected the subscription controllers' error responses, matched {len(covered)}"

    offenders = sorted(
        f"{method.upper()} {path} -> {status}"
        for (path, method, status) in covered
        if "ResponseMessage" in declared[(path, method, status)]
    )
    assert not offenders, f"ResponseMessage declared on subscription error responses: {offenders}"


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
