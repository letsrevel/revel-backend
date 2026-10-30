"""The third-party issuance statement is carried as the EN 16931 invoice note, BT-22 (#1258)."""

import pytest

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Event, Organization
from events.service import einvoice_mapping as mapping
from events.tests.test_einvoice._fixtures import line, make_credit_note, make_invoice

pytestmark = pytest.mark.django_db


def test_invoice_note_is_the_snapshot_statement(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], issuer_name="Revel e.U.")
    draft = mapping.invoice_draft_for(invoice)
    assert [note.note for note in draft.notes] == ["Issued by Revel e.U. in the name and on behalf of ACME GmbH."]


def test_credit_note_carries_its_own_snapshot(organization: Organization, event: Event, member_user: RevelUser) -> None:
    invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")], issuer_name="Revel e.U.")
    note = make_credit_note(invoice, [line("24.90", "13.00")])
    note.issuer_name = "Renamed e.U."
    note.save()
    draft = mapping.credit_note_draft_for(note)
    assert [n.note for n in draft.notes] == ["Issued by Renamed e.U. in the name and on behalf of ACME GmbH."]


def test_legacy_document_without_a_snapshot_names_the_current_operator(
    organization: Organization, event: Event, member_user: RevelUser
) -> None:
    settings = SiteSettings.get_solo()
    settings.platform_business_name = "Revel Platform e.U."
    settings.save()
    invoice = make_invoice(organization, event, member_user, lines=[line("24.90", "13.00")])
    assert invoice.issuer_name == ""
    draft = mapping.invoice_draft_for(invoice)
    assert draft.notes[0].note == "Issued by Revel Platform e.U. in the name and on behalf of ACME GmbH."
