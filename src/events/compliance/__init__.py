"""Pluggable per-country fiscal compliance (EU layer 1, #1057-#1067).

- :mod:`.base` — the :class:`CountryCompliancePolicy` hooks, the permissive
  :class:`DefaultEUPolicy`, and the reusable restriction mixins.
- :mod:`.registry` — ``@register("XX")`` and :func:`get_policy` (organization →
  resolved country → policy; unknown or unset country → the EU default).
- :mod:`.policies` — one module per country, auto-discovered.
- :mod:`.enforcement` — country-agnostic helpers the services call.

Call sites ask the resolved policy only; none of them branches on a country code.
Lives inside ``events`` rather than as its own app: it has no models, and its hooks
take ``events`` objects (organizations, tickets, tiers), which ``common`` may not import.
"""

from events.compliance import policies as _policies  # noqa: F401  (registers every country)
from events.compliance.base import (
    AttendeeInvoicingCapability,
    BuyerContext,
    CountryCompliancePolicy,
    Decision,
    Nexus,
    DefaultEUPolicy,
    PaymentChannelCapability,
    TicketComplianceField,
)
from events.compliance.registry import (
    get_policy,
    get_policy_for_country,
    register,
    registered_policies,
    resolve_org_country,
)

__all__ = [
    "AttendeeInvoicingCapability",
    "BuyerContext",
    "CountryCompliancePolicy",
    "Decision",
    "Nexus",
    "DefaultEUPolicy",
    "PaymentChannelCapability",
    "TicketComplianceField",
    "get_policy",
    "get_policy_for_country",
    "register",
    "registered_policies",
    "resolve_org_country",
]
