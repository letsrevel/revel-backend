"""Switch attendee invoicing off where the country policy now blocks it (EU layer 1).

Organizations established in HR, ES, PT, SI, GR, RO or HU with HYBRID/AUTO invoicing
are moved to NONE, so the stored setting matches what Revel will actually do (the
generation gate already refuses those invoices). Existing invoices are untouched. BE
and PL keep their mode: only domestic B2B invoices are skipped there.

The country is resolved like ``events.compliance.registry.resolve_country`` at the time
of writing: declared VAT country, then VAT ID prefix, then the organization's city.
"""

import typing as t

from django.db import migrations

_BLOCKED = frozenset({"HR", "ES", "PT", "SI", "GR", "RO", "HU"})


def _country(vat_country_code: str, vat_id: str, city_iso2: str | None) -> str:
    code = (vat_country_code or "").strip().upper()
    if not code:
        prefix = (vat_id or "").strip()[:2].upper()
        code = prefix if len(prefix) == 2 and prefix.isalpha() else (city_iso2 or "").upper()
    return "GR" if code == "EL" else code


def disable(apps: t.Any, schema_editor: t.Any) -> None:
    """Set invoicing_mode to NONE for organizations in blocked countries."""
    Organization = apps.get_model("events", "Organization")
    rows = Organization.objects.exclude(invoicing_mode="none").values_list(
        "pk", "vat_country_code", "vat_id", "city__iso2"
    )
    blocked = [pk for pk, vat_country_code, vat_id, iso2 in rows if _country(vat_country_code, vat_id, iso2) in _BLOCKED]
    Organization.objects.filter(pk__in=blocked).update(invoicing_mode="none")


class Migration(migrations.Migration):
    dependencies = [("events", "0127_backfill_ticket_numbers")]

    # Not reversible in a meaningful way (the previous mode is not recorded): noop.
    operations = [migrations.RunPython(disable, reverse_code=migrations.RunPython.noop)]
