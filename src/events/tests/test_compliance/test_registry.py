"""Registry, country resolution and the policy contract (EU layer 1)."""

import importlib
import inspect
import pkgutil

import pytest
from django.contrib.gis.geos import Point
from django.core.exceptions import ImproperlyConfigured

from events.compliance import (
    AttendeeInvoicingCapability,
    BuyerContext,
    ComplianceNotice,
    CountryCompliancePolicy,
    Decision,
    DefaultEUPolicy,
    Nexus,
    NoticeTopic,
    PaymentChannelCapability,
    TicketComplianceField,
    get_policy,
    get_policy_for_country,
    register,
    registered_policies,
    registry,
)
from events.compliance import policies as policies_package
from events.compliance.base import ALL_NEXUS
from events.compliance.enforcement import ticket_fields
from events.compliance.registry import resolve_country
from events.models import Organization, Ticket
from geo.models import City

pytestmark = pytest.mark.django_db

EXPECTED_COUNTRIES = {"AT", "BE", "DK", "ES", "FR", "GR", "HR", "HU", "IT", "PL", "PT", "RO", "SI"}
# ISO 3166-2 subdivisions with their own policy, defined in their country's module (#1086).
EXPECTED_SUBDIVISIONS = {"ES-NC", "ES-PV"}
EXPECTED_CODES = EXPECTED_COUNTRIES | EXPECTED_SUBDIVISIONS


class TestCountryResolution:
    def test_declared_vat_country_wins(self) -> None:
        assert resolve_country("es", "IT12345678901", "DE") == "ES"

    def test_vat_id_prefix_is_the_fallback(self) -> None:
        assert resolve_country("", "PT123456789", "DE") == "PT"

    def test_city_is_the_last_resort(self) -> None:
        assert resolve_country("", "", "hr") == "HR"

    def test_nothing_declared_resolves_to_empty(self) -> None:
        assert resolve_country("", "", None) == ""

    def test_greek_vat_prefix_maps_to_iso(self) -> None:
        assert resolve_country("EL", "", "") == "GR"
        assert resolve_country("", "EL123456789", "") == "GR"

    def test_org_city_feeds_resolution(self, organization: Organization) -> None:
        organization.city = City.objects.create(
            name="Zagreb",
            ascii_name="Zagreb",
            country="Croatia",
            iso2="HR",
            iso3="HRV",
            city_id=91001,
            location=Point(15.98, 45.81),
        )
        organization.save()

        assert get_policy(organization).country == "HR"
        assert get_policy(organization).attendee_invoicing_capability() == AttendeeInvoicingCapability.BLOCKED


class TestRegistry:
    def test_every_researched_country_is_registered(self) -> None:
        assert set(registered_policies()) == EXPECTED_CODES

    def test_unknown_or_unset_country_gets_the_default(self) -> None:
        for code in ("DE", "US", "", None):
            policy = get_policy_for_country(code)
            assert type(policy) is DefaultEUPolicy
            assert policy.attendee_invoicing(BuyerContext(vat_country="DE"), ALL_NEXUS).allowed
            assert policy.online_payment(ALL_NEXUS).allowed
            assert policy.offline_payment(ALL_NEXUS).allowed

    def test_policy_is_bound_to_the_resolved_country(self) -> None:
        assert get_policy_for_country("el").country == "GR"

    def test_every_policy_module_is_auto_discovered(self) -> None:
        """Each module in ``policies/`` registers the country it is named after (plus that country's subdivisions)."""
        modules = [m.name for m in pkgutil.iter_modules(policies_package.__path__)]
        assert sorted({code[:2].lower() for code in registered_policies()}) == sorted(modules)

    def test_duplicate_registration_is_refused(self) -> None:
        with pytest.raises(ImproperlyConfigured):

            @register("IT")
            class _Another(DefaultEUPolicy):
                pass

    def test_malformed_country_is_refused(self) -> None:
        with pytest.raises(ImproperlyConfigured):
            register("ITA")

    def test_a_new_country_needs_only_a_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Registering a class is all it takes: resolution and capabilities follow, no call site changes."""
        monkeypatch.setattr(registry, "_REGISTRY", dict(registry._REGISTRY))

        @register("XK")
        class _NewCountryPolicy(DefaultEUPolicy):
            def online_payment(self, nexus: frozenset[Nexus]) -> Decision:
                return Decision.block("nope")

        policy = get_policy_for_country("xk")
        assert isinstance(policy, _NewCountryPolicy)
        assert policy.online_payment_capability() == PaymentChannelCapability.BLOCKED


@pytest.mark.parametrize("code", sorted(EXPECTED_CODES))
class TestPolicyContract:
    """Every registered policy implements every hook with the right types."""

    def test_is_a_concrete_policy(self, code: str) -> None:
        cls = registered_policies()[code]
        assert issubclass(cls, CountryCompliancePolicy)
        assert not inspect.isabstract(cls)
        assert cls.__module__ == f"events.compliance.policies.{code[:2].lower()}"

    def test_capabilities_are_derived_not_overridden(self, code: str) -> None:
        """The FE capabilities are computed from the hooks, so they can never disagree with enforcement."""
        cls = registered_policies()[code]
        for name in ("attendee_invoicing_capability", "online_payment_capability", "offline_payment_capability"):
            assert name not in vars(cls), name
        policy = cls(code)
        assert isinstance(policy.attendee_invoicing_capability(), AttendeeInvoicingCapability)
        assert isinstance(policy.online_payment_capability(), PaymentChannelCapability)
        assert isinstance(policy.offline_payment_capability(), PaymentChannelCapability)

    @pytest.mark.parametrize("nexus", [frozenset({Nexus.ESTABLISHMENT}), frozenset({Nexus.VENUE}), ALL_NEXUS])
    def test_decisions_are_well_formed(self, code: str, nexus: frozenset[Nexus]) -> None:
        policy = get_policy_for_country(code)
        for decision in (
            policy.attendee_invoicing(BuyerContext(), nexus),
            policy.attendee_invoicing(BuyerContext(vat_country=code[:2]), nexus),
            policy.online_payment(nexus),
            policy.offline_payment(nexus),
        ):
            assert isinstance(decision, Decision)
            # A refusal always explains itself to the user.
            assert decision.allowed or decision.reason

    @pytest.mark.parametrize("nexus", [frozenset({Nexus.ESTABLISHMENT}), frozenset({Nexus.VENUE}), ALL_NEXUS])
    def test_organizer_notices_are_well_formed(self, code: str, nexus: frozenset[Nexus]) -> None:
        notices = get_policy_for_country(code).organizer_notices(nexus)
        assert all(isinstance(n, ComplianceNotice) and n.key and n.message for n in notices)
        assert all(isinstance(n.applies_to, NoticeTopic) for n in notices)

    def test_extra_ticket_fields_are_well_formed(self, code: str, ticket: Ticket) -> None:
        fields = get_policy_for_country(code).extra_ticket_fields(ticket, ALL_NEXUS)
        assert all(isinstance(field, TicketComplianceField) for field in fields)
        assert not {"organizer", "price", "notice"} & {field.key for field in fields}


def test_common_ticket_fields_always_come_first(ticket: Ticket) -> None:
    """Countries add lines; the common set is assembled outside any policy, so none can drop it."""
    keys = [field.key for field in ticket_fields(ticket)]
    assert keys[0] == "organizer"
    assert {"organizer", "price", "notice"} <= set(keys)


def test_offline_payment_is_never_restricted_in_layer_1() -> None:
    for code in EXPECTED_COUNTRIES:
        assert get_policy_for_country(code).offline_payment_capability() == PaymentChannelCapability.ALLOWED


def test_registry_holds_the_module_classes() -> None:
    """The registry entry is the class defined in the country module."""
    module = importlib.import_module("events.compliance.policies.it")
    assert registered_policies()["IT"] is module.ItalyPolicy
