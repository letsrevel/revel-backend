"""Apple Wallet passes render in the holder's language, not the caller's (#1217)."""

import json
import typing as t
import zipfile
from io import BytesIO
from unittest.mock import MagicMock

import pytest
from django.utils import translation

from wallet.apple.generator import ApplePassGenerator

pytestmark = pytest.mark.django_db


def _pass_json(pkpass: bytes) -> dict[str, t.Any]:
    with zipfile.ZipFile(BytesIO(pkpass)) as zf:
        return t.cast(dict[str, t.Any], json.loads(zf.read("pass.json")))


def _labels(fields: list[dict[str, t.Any]]) -> list[str]:
    return [f["label"] for f in fields if "label" in f]


@pytest.fixture
def generator(mock_signer: MagicMock) -> ApplePassGenerator:
    return ApplePassGenerator(signer=mock_signer)


def _generate(generator: ApplePassGenerator, ticket: t.Any, language: str) -> dict[str, t.Any]:
    ticket.user.language = language
    ticket.user.save()
    ticket.refresh_from_db()
    return _pass_json(generator.generate_pass(ticket))


def test_german_holder_gets_german_labels_and_dates(generator: ApplePassGenerator, ticket: t.Any) -> None:
    et = _generate(generator, ticket, "de")["eventTicket"]

    assert _labels(et["auxiliaryFields"]) == ["TICKET", "PREIS", "GAST"]
    assert _labels(et["secondaryFields"]) == ["ADRESSE"]
    back = {f["key"]: f for f in et["backFields"]}
    assert back["ticket_id"]["label"] == "Ticket-ID"
    assert back["powered_by"]["label"] == "Bereitgestellt von"
    assert "Beginn: " in back["event_details"]["value"]
    assert "Ende: " in back["event_details"]["value"]
    # Header date is "j. M, H:i", not the English "M j, H:i" ("Jan 3, 19:00").
    assert "." in et["headerFields"][0]["value"]


def test_request_language_does_not_leak_into_holder_pass(generator: ApplePassGenerator, ticket: t.Any) -> None:
    ticket.user.language = "de"
    ticket.user.save()
    ticket.refresh_from_db()

    baseline = _pass_json(generator.generate_pass(ticket))
    with translation.override("fr"):
        under_fr = _pass_json(generator.generate_pass(ticket))

    assert under_fr == baseline
    assert baseline["eventTicket"]["auxiliaryFields"][0]["label"] == "TICKET"
    assert baseline["eventTicket"]["auxiliaryFields"][1]["label"] == "PREIS"


def test_english_holder_output_is_unchanged_under_foreign_request_language(
    generator: ApplePassGenerator, ticket: t.Any
) -> None:
    with translation.override("de"):
        pass_json = _generate(generator, ticket, "en")

    et = pass_json["eventTicket"]
    assert _labels(et["auxiliaryFields"]) == ["TICKET", "PRICE", "GUEST"]
    back = {f["key"]: f for f in et["backFields"]}
    assert back["powered_by"]["label"] == "Powered by"
    assert back["event_details"]["value"].split("\n\n")[1].startswith("Start: ")


def test_membership_pass_uses_holder_language(generator: ApplePassGenerator, member: t.Any) -> None:
    member.user.language = "de"
    member.user.save()
    member.refresh_from_db()

    with translation.override("fr"):
        pass_json = _pass_json(generator.generate_membership_pass(member))

    labels = _labels(pass_json["generic"]["secondaryFields"])
    assert "MITGLIED SEIT" in labels
    assert _labels(pass_json["generic"]["backFields"])[0] == "Mitglieds-ID"
