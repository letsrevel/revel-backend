"""In-app / Telegram ``.md`` templates are translated from the catalog (#1400, #1401)."""

from django.template.loader import render_to_string
from django.utils.translation import override


def _render(template: str, language: str) -> str:
    context = {"event_name": "Summer Fest", "days_until": 1, "event_url": "https://example.test/e"}
    with override(language):
        return render_to_string(f"notifications/{template}/event_reminder.md", {"context": context})


def test_in_app_md_template_uses_the_german_catalog_entry() -> None:
    assert "Erinnerung: **Summer Fest** ist morgen!" in _render("in_app", "de")


def test_telegram_md_template_uses_the_french_catalog_entry() -> None:
    assert "Rappel : <b>Summer Fest</b> est demain !" in _render("telegram", "fr")
