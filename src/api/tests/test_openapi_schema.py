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
