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
- set the class attributes the mixin needs (and, if the rule starts in the future, its start date);
- write a module docstring that names the issue and links the docs page.

```python
"""Narnia (#1234). Consumer receipts must be fiscalized in real time with the tax office.

Docs: https://docs.letsrevel.io/compliance/eu/xx/ (docs/compliance/eu/xx.md).
"""

import datetime

from events.compliance.base import DefaultEUPolicy, FiscalizedInvoicingMixin
from events.compliance.registry import register


@register("XX")
class NarniaPolicy(FiscalizedInvoicingMixin, DefaultEUPolicy):
    """Narnia: see the module docstring."""

    fiscal_system = "the national fiscalization service"
    fiscal_invoicing_from = datetime.date(2027, 7, 1)
```

The mixin goes **before** `DefaultEUPolicy` so its hooks win. Registering two policies for the same
country, or a malformed code, raises `ImproperlyConfigured` at startup.

### Nexus and effective dates

Every hook receives `nexus: frozenset[Nexus]`: the ways this country reaches the sale being decided.

- `Nexus.ESTABLISHMENT`: the organizer is established in the country (the organization's resolved
  country).
- `Nexus.VENUE`: the physical (non-virtual) event takes place in the country (the event's VAT
  country). Virtual events never give a venue nexus.

Each restriction declares which nexus it applies on and from when. The generic helper
`in_force(nexus, applies_on, since)` in `base.py` is true only when the sale reaches the country
through one of `applies_on` and `since` is `None` (already in force) or today is on or after it. Use it
for any custom hook too, so the scope and start date stay declarative.

Scope a restriction to what the law reaches, nothing broader: for example, an invoicing rule that
binds only sellers established in the country applies on `{ESTABLISHMENT}`; a rule that reaches any
seller of a supply there (`ALL_NEXUS`) also covers physical events held there by foreign organizers;
a territorial ticketing rule applies on `{VENUE}`.

### Available mixins

| Mixin | Effect | Class attributes |
|---|---|---|
| `FiscalizedInvoicingMixin` | Blocks Revel-issued attendee invoices for every buyer. | `fiscal_system`; `fiscal_invoicing_applies_on` (default `{ESTABLISHMENT}`); `fiscal_invoicing_from` (default `None`) |
| `B2BEInvoicingMixin` | Blocks attendee invoices to business buyers (VAT ID valid in VIES or unverifiable). `b2b_buyer_scope`: `DOMESTIC` (same-country VAT ID only, e.g. Belgium) or `ANY_BUSINESS` (e.g. Poland). Applies to `ESTABLISHMENT` only. | `e_invoicing_network`; `b2b_buyer_scope` (default `DOMESTIC`); `e_invoicing_from` (default `None`) |
| `CertifiedOnlineTicketingMixin` | Blocks online (Stripe card) payment for paid tickets and series passes. Offline and at-the-door payments, free tickets, RSVPs and memberships are unaffected. | `certified_system`; `online_ticketing_applies_on` (default `{VENUE}`); `online_ticketing_from` (default `None`) |

The system or network attribute is interpolated into the translated refusal message, together with
the localized country name.

### Capabilities are derived

The organization admin API exposes `compliance: {country, attendee_invoicing, online_payment,
offline_payment}`. These values come from the final methods `attendee_invoicing_capability()`,
`online_payment_capability()` and `offline_payment_capability()` on `CountryCompliancePolicy`, which
evaluate the hooks for the organization's own country at today's date. **Do not override them**: they
are derived from the hooks so that they can never disagree with what is enforced, and a future-dated
restriction reads `allowed` until it starts.

A country with no restriction but with a page and later plans (for example France) can register a
plain `DefaultEUPolicy` subclass, which gives later layers a place to hook in.

### Country-specific behaviour

If no mixin fits, override the hooks directly:

- `attendee_invoicing(buyer: BuyerContext, nexus: frozenset[Nexus]) -> Decision`
- `online_payment(nexus: frozenset[Nexus]) -> Decision`
- `offline_payment(nexus: frozenset[Nexus]) -> Decision`
- `extra_ticket_fields(ticket, nexus: frozenset[Nexus]) -> list[TicketComplianceField]`
- `organizer_notices(nexus: frozenset[Nexus]) -> list[ComplianceNotice]`: non-blocking hints
  (`key`, `applies_to: NoticeTopic`, translated `message`), exposed in the org and event
  `compliance` objects. Use them when the law puts a duty on the organizer that Revel can't and
  shouldn't enforce (Austria, Denmark, Poland), or to tell the organizer what to do instead of a
  feature Revel blocks (Croatia, Slovenia, Greece, Hungary). Pick the `NoticeTopic` of the setting the
  duty concerns: `offline_payment` for money taken at the venue, `ticket_sales` when it covers online
  sales too, `attendee_invoicing` for who issues the attendee invoices. A notice that explains a block
  should reach the same sales: `FiscalizedInvoicingMixin.fiscal_invoicing_in_force(nexus)` gives that
  scope.

Return `Decision.block(reason)` with a translated, user-facing reason when refusing.

`extra_ticket_fields` adds country-specific lines to ticket PDFs and wallet passes. The policy has no
`ticket_fields` method: the module-level `events.compliance.enforcement.ticket_fields(ticket)` builds
the EU common set first and then appends each reaching country's `extra_ticket_fields(ticket, nexus)`,
so a country can add lines but never remove the common ones. Check `nexus` to scope the line (Italy,
for example, adds its notice only for events held there).

## 2. Document it

- Add `docs/compliance/eu/<cc>.md` following the template of the existing country pages (Status, What
  Revel does, Legal basis, Open questions, Later layers).
- Update the status table in `docs/compliance/eu/index.md`.
- Add the page to the `Compliance > EU` section of `mkdocs.yml` (alphabetical by English name).

## 3. Test it

- The registry contract test (`src/events/tests/test_compliance/test_registry.py`) checks
  automatically that every registered policy is concrete, returns well-formed decisions for every
  nexus, does not override the derived capabilities, keeps the common ticket lines first, and lives in
  a module named after its country. Add the new code to its
  `EXPECTED_COUNTRIES` set.
- Add a per-country decision test in `src/events/tests/test_compliance/test_policies.py` that asserts
  what the policy allows and blocks (for example: a consumer buyer, a domestic business buyer, a
  foreign business buyer, online and offline payment, each nexus, and the day before and on the start
  date of a future-dated restriction).

## Adding a new hook

When a later layer needs a new decision (a fiscalization provider, a retention period, an export):

1. Add an abstract method to `CountryCompliancePolicy`.
2. Give it a permissive default in `DefaultEUPolicy`.
3. Call it from `enforcement.py` or the relevant service, never with a country check at the call site.

Existing countries inherit the default unchanged; only the countries that need it override it.
