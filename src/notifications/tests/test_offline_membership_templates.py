"""Offline-membership notification templates: markdown instructions, accepted-application lead, lapse CTA."""

import typing as t

import pytest
from django.template.loader import render_to_string
from django.utils import translation

from common.fields import render_markdown
from notifications.utils import sanitize_for_telegram

pytestmark = pytest.mark.django_db

CHANNELS = {
    "html": "notifications/email/{}.html",
    "txt": "notifications/email/{}.txt",
    "in_app": "notifications/in_app/{}.md",
    "telegram": "notifications/telegram/{}.md",
}
SCRIPT = "<script>alert(1)</script>"


def _render(name: str, channel: str, **context: t.Any) -> str:
    base: dict[str, t.Any] = {
        "organization_name": "Acme Club",
        "organization_contact_url": "https://example.com/org/acme",
        "plan_name": "Annual",
        "tier_name": "Gold",
        "amount": "50.00 EUR",
        "period_end": "2027-01-01",
        "expired_at": "1 Jan 2027",
    }
    base.update(context)
    with translation.override("en"):
        return render_to_string(CHANNELS[channel].format(name), {"user": {"display_name": "Sam"}, "context": base})


INSTRUCTIONS = "## How to pay\n\nPay by **IBAN** AT12:\n\n- cash is fine\n\n" + SCRIPT


# ---- 3. markdown instructions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "extra"),
    [
        ("membership_payment_requested", {"memberships_url": "https://x/account/memberships"}),
        ("subscription_renewal_reminder", {"is_online": False}),
    ],
)
class TestInstructionsAreMarkdown:
    def test_email_renders_markdown_to_html(self, name: str, extra: dict[str, t.Any]) -> None:
        html = _render(name, "html", payment_instructions=INSTRUCTIONS, **extra)

        assert "<strong>IBAN</strong>" in html
        assert "<li>cash is fine</li>" in html
        assert "How to pay" in html  # staff-authored content is never stripped (a heading may be the content)

    @pytest.mark.parametrize("channel", ["html", "in_app", "telegram"])
    def test_script_never_survives(self, name: str, extra: dict[str, t.Any], channel: str) -> None:
        body = _render(name, channel, payment_instructions=INSTRUCTIONS, **extra)
        if channel == "telegram":  # the real pipeline: markdown -> html -> telegram subset
            body = sanitize_for_telegram(render_markdown(body))

        assert "<script" not in body.lower()
        if channel != "html":  # the email sanitizer drops the element wholesale; the chat channels escape it
            assert "alert(1)" in body

    @pytest.mark.parametrize("channel", ["in_app", "telegram"])
    def test_chat_channels_keep_the_instructions(self, name: str, extra: dict[str, t.Any], channel: str) -> None:
        body = _render(name, channel, payment_instructions="Pay by IBAN AT12", **extra)
        assert "Pay by IBAN AT12" in body

    def test_leading_heading_is_content_not_stripped(self, name: str, extra: dict[str, t.Any]) -> None:
        body = _render(name, "in_app", payment_instructions="# Bank account 12345\n\nAT12", **extra)
        assert "Bank account 12345" in body

    def test_telegram_markdown_link_is_clickable_and_safe(self, name: str, extra: dict[str, t.Any]) -> None:
        raw = _render(
            name, "telegram", payment_instructions="[Pay here](https://pay.example/x?a=1&b=2) <b>x</b>", **extra
        )
        body = sanitize_for_telegram(render_markdown(raw))

        assert '<a href="https://pay.example/x?a=1&amp;b=2"' in body
        assert ">Pay here</a>" in body
        assert "<b>x</b>" not in body  # staff HTML is escaped, never passed through

    def test_plain_text_is_unchanged(self, name: str, extra: dict[str, t.Any]) -> None:
        body = _render(name, "txt", payment_instructions="Pay by IBAN AT12", **extra)
        assert "Pay by IBAN AT12" in body


# ---- 8. payment requested CTA + accepted lead -----------------------------------------------------------


@pytest.mark.parametrize("channel", list(CHANNELS))
def test_payment_requested_cta_and_accepted_lead(channel: str) -> None:
    base: dict[str, t.Any] = {"memberships_url": "https://platform.example/account/memberships"}

    plain = _render("membership_payment_requested", channel, **base)
    accepted = _render("membership_payment_requested", channel, application_accepted=True, **base)

    assert "Your memberships" in plain
    assert "https://platform.example/account/memberships" in plain
    assert "accepted your application" not in plain
    assert "Acme Club" in accepted.split("accepted your application")[0][-40:]
    assert "accepted your application" in accepted
    # The lead comes first: before the payment ask.
    assert accepted.index("accepted your application") < accepted.index("pay")


# ---- 9. lapse email -------------------------------------------------------------------------------------


@pytest.mark.parametrize("channel", list(CHANNELS))
def test_lapse_links_to_org_membership_page(channel: str) -> None:
    url = "https://platform.example/org/acme/membership"

    lapsed = _render("subscription_expired", channel, lapsed=True, membership_url=url)
    ended = _render("subscription_expired", channel, membership_url=url)

    assert url in lapsed
    assert "join or apply again" in lapsed.lower()
    assert "start a new subscription" not in lapsed
    assert url not in ended


# ---- 10. membership invitation ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", list(CHANNELS))
def test_membership_invitation_renders_accept_link_and_expiry(channel: str) -> None:
    url = "https://platform.example/account/membership-invitations/tok123"

    body = _render("membership_invitation_received", channel, accept_url=url, expires_at="2 Dec 2026, 15:30 CET")

    assert "Acme Club" in body
    assert "Gold" in body
    assert url in body
    assert "2 Dec 2026, 15:30 CET" in body
    assert "only become a member if you accept" in body
    assert "None" not in body
    assert "{{" not in body


def test_membership_invitation_sets_membership_and_notification_policies() -> None:
    from notifications.enums import MANDATORY_TYPES, TENANT_ORG_SENDER_TYPES, TRANSACTIONAL_TYPES, NotificationType

    ntype = NotificationType.MEMBERSHIP_INVITATION_RECEIVED
    assert ntype in TRANSACTIONAL_TYPES
    assert ntype in TENANT_ORG_SENDER_TYPES
    assert ntype not in MANDATORY_TYPES
