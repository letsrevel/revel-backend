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


def test_whole_sentence_template_is_translated_with_natural_word_order() -> None:
    """The verb phrase is one msgid, so German puts the organization before the verb (#1400)."""
    context = {"requester_name": "Ada", "organization_name": "Chess Club", "frontend_url": "https://example.test/r"}
    with override("de"):
        rendered = render_to_string("notifications/in_app/membership_request_created.md", {"context": context})
    assert "**Ada** hat eine Mitgliedschaft bei **Chess Club** beantragt." in rendered


def test_contextual_single_word_is_translated() -> None:
    """Single-word headings carry a msgctxt, and the .po entry matches it."""
    context = {"event_url": "https://example.test/e", "item_name": "Cake", "event_name": "Fest"}
    with override("de"):
        rendered = render_to_string("notifications/in_app/potluck_item_claimed.md", {"context": context})
    assert "Beansprucht" in rendered
