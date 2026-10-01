# Adding a Country

Adding a country is adding one file. Call sites (services, controllers, tasks) never change: they
only talk to the policy object returned by `get_policy(org)` or `get_policy_for_country(code)`.

## 1. Create the policy module

Create `src/events/compliance/policies/<cc>.py`, where `<cc>` is the lower-case ISO 3166-1 alpha-2
code. Every module in `policies/` is imported at startup, so there is no list to update.

In it:

- subclass `DefaultEUPolicy`, optionally through one of the mixins in `base.py`;
- decorate the class with `@register("CC")` (upper-case ISO code; `EL` is accepted and normalized to
  `GR`);
- set the class attribute the mixin needs;
- write a module docstring that names the issue and links the docs page.

```python
"""Narnia (#1234). Consumer receipts must be fiscalized in real time with the tax office.

Docs: https://docs.letsrevel.io/compliance/eu/xx/ (docs/compliance/eu/xx.md).
"""

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("XX")
class NarniaPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Narnia: see the module docstring."""

    fiscal_system = "the national fiscalization service"
```

The mixin goes **before** `DefaultEUPolicy` so its hooks win. Registering two policies for the same
country, or a malformed code, raises `ImproperlyConfigured` at startup.

### Available mixins

| Mixin | Effect | Class attribute |
|---|---|---|
| `FiscalizedInvoicingMixin` | Blocks Revel-issued attendee invoices for every buyer. | `fiscal_system` |
| `DomesticB2BEInvoicingMixin` | Blocks attendee invoices only when the buyer's VAT ID is from the same country. | `e_invoicing_network` |
| `CertifiedTicketingMixin` | Blocks paid ticketing. Free tickets, RSVPs and memberships are unaffected. | `certified_system` |

The class attribute is interpolated into the translated refusal message the organizer sees. Each
mixin also sets the matching capability (`AttendeeInvoicingCapability` or `PaidTicketingCapability`)
that the organization admin API exposes to the frontend.

A country with no restriction but with a page and later plans (for example France) can register a
plain `DefaultEUPolicy` subclass, which gives later layers a place to hook in.

### Country-specific behaviour

If no mixin fits, override the hooks directly:

- `attendee_invoicing(buyer: BuyerContext) -> Decision`
- `paid_ticketing() -> Decision`
- `extra_ticket_fields(ticket) -> list[TicketComplianceField]`

`extra_ticket_fields` adds country-specific lines to ticket PDFs and wallet passes. They are appended
after the common EU set by `ticket_fields`, which is final: a country can add lines but never remove
the common ones. Return `Decision.block(reason)` with a translated, user-facing reason when refusing.

## 2. Document it

- Add `docs/compliance/eu/<cc>.md` following the template of the existing country pages (Status, What
  Revel does, Legal basis, Open questions, Later layers).
- Update the status table in `docs/compliance/eu/index.md`.
- Add the page to the `Compliance > EU` section of `mkdocs.yml` (alphabetical by English name).

## 3. Test it

- The registry contract test (`src/events/tests/test_compliance/test_registry.py`) checks
  automatically that every registered policy implements all hooks, declares its capabilities, keeps
  the common ticket lines, and lives in a module named after its country. Add the new code to its
  `EXPECTED_COUNTRIES` set.
- Add a per-country decision test in `src/events/tests/test_compliance/test_policies.py` that asserts
  what the policy allows and blocks (for example: a consumer buyer, a domestic business buyer, a
  foreign business buyer, paid ticketing).

## Adding a new hook

When a later layer needs a new decision (a fiscalization provider, a retention period, an export):

1. Add an abstract method to `CountryCompliancePolicy`.
2. Give it a permissive default in `DefaultEUPolicy`.
3. Call it from `enforcement.py` or the relevant service, never with a country check at the call site.

Existing countries inherit the default unchanged; only the countries that need it override it.
