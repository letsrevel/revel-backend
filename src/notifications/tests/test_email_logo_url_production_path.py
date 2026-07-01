"""Production-path regression tests for the branded email logo URL.

The brand header (_brand_header.html) renders:
    <img src="{{ frontend_base_url }}/revel-email-logo.png">

These tests verify that `frontend_base_url` is injected at the TOP LEVEL of the
template context on EVERY outbound email path that goes through our own rendering
code (notification templates and digest).  They MUST fail if `frontend_base_url`
is missing from the context and PASS once the fix is in place.

Previously, a guard test (test_email_templates_branding.py) hand-built the context
and always passed even when the real render path did NOT inject the key.  These
tests call the real methods that the EmailChannel invokes at runtime.
"""


import pytest

from accounts.models import RevelUser
from notifications.enums import NotificationType
from notifications.models import Notification
from notifications.service.digest import NotificationDigest
from notifications.service.templates.registry import get_template

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _notification_logo_src(html: str) -> str:
    """Return the value of src= for the logo img tag, or empty string."""
    marker = 'revel-email-logo.png"'
    idx = html.find(marker)
    if idx == -1:
        return ""
    # Walk back from marker to find the opening quote of the src attribute
    snippet = html[:idx]
    src_start = snippet.rfind('src="')
    if src_start == -1:
        return ""
    return html[src_start + 5 : idx + len(marker) - 1]


# ---------------------------------------------------------------------------
# Notification path
# ---------------------------------------------------------------------------


class TestNotificationEmailLogoUrl:
    """The real _get_template_context() must inject frontend_base_url."""

    def test_logo_img_src_is_absolute_url(
        self,
        notification: Notification,
    ) -> None:
        """get_email_html_body() must produce an absolute src for the logo.

        This test exercises the same code-path that EmailChannel calls at
        runtime.  It will FAIL if _get_template_context() does not inject
        `frontend_base_url` (the logo src becomes "/revel-email-logo.png").
        """
        template = get_template(NotificationType.TICKET_CREATED)
        html = template.get_email_html_body(notification)
        assert html is not None, "get_email_html_body() returned None"

        assert "revel-email-logo.png" in html, "Brand logo is missing from the email entirely"

        src = _notification_logo_src(html)
        assert src.startswith("http"), (
            f"Logo src is host-relative (no scheme+host): got {src!r}. "
            "frontend_base_url is not being injected into the notification render context."
        )

    def test_logo_src_contains_frontend_base_url(
        self,
        notification: Notification,
    ) -> None:
        """The logo src must start with the site's frontend_base_url."""
        from common.models import SiteSettings

        expected_base = SiteSettings.get_solo().frontend_base_url

        template = get_template(NotificationType.TICKET_CREATED)
        html = template.get_email_html_body(notification)
        assert html is not None

        src = _notification_logo_src(html)
        assert src.startswith(expected_base), (
            f"Logo src {src!r} does not start with frontend_base_url {expected_base!r}."
        )

    def test_logo_src_not_host_relative(
        self,
        notification: Notification,
    ) -> None:
        """Explicit negative: src must NOT be /revel-email-logo.png (host-relative)."""
        template = get_template(NotificationType.TICKET_CREATED)
        html = template.get_email_html_body(notification)
        assert html is not None

        assert 'src="/revel-email-logo.png"' not in html, (
            'Logo src is host-relative: src="/revel-email-logo.png" found in rendered HTML. '
            "frontend_base_url is missing from the notification render context."
        )


# ---------------------------------------------------------------------------
# Digest path
# ---------------------------------------------------------------------------


class TestDigestEmailLogoUrl:
    """build_digest_content() must inject frontend_base_url so the logo is absolute."""

    def test_digest_logo_img_src_is_absolute_url(
        self,
        digest_notifications: list[Notification],
        regular_user: RevelUser,
    ) -> None:
        """Digest HTML must contain an absolute logo URL.

        This test exercises NotificationDigest.build_digest_content() — the same
        method the beat task calls.  It will FAIL if digest.py does not inject
        `frontend_base_url` into the render context.
        """
        from django.db.models import QuerySet

        qs: QuerySet[Notification] = Notification.objects.filter(id__in=[n.id for n in digest_notifications])
        service = NotificationDigest(user=regular_user, notifications=qs)
        _subject, _text, html = service.build_digest_content()

        assert "revel-email-logo.png" in html, "Brand logo is missing from the digest email entirely"

        src = _notification_logo_src(html)
        assert src.startswith("http"), (
            f"Digest logo src is host-relative (no scheme+host): got {src!r}. "
            "frontend_base_url is not being injected into the digest render context."
        )

    def test_digest_logo_src_not_host_relative(
        self,
        digest_notifications: list[Notification],
        regular_user: RevelUser,
    ) -> None:
        """Explicit negative: digest src must NOT be /revel-email-logo.png."""
        from django.db.models import QuerySet

        qs: QuerySet[Notification] = Notification.objects.filter(id__in=[n.id for n in digest_notifications])
        service = NotificationDigest(user=regular_user, notifications=qs)
        _subject, _text, html = service.build_digest_content()

        assert 'src="/revel-email-logo.png"' not in html, (
            'Digest logo src is host-relative: src="/revel-email-logo.png" found in rendered HTML. '
            "frontend_base_url is missing from the digest render context."
        )
