"""Webhook endpoint for email-provider events (bounces, complaints)."""

from django.conf import settings
from django.http import Http404, HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from notifications.service import email_events


@api_controller("/email-events", tags=["Email Events"], auth=None)
class EmailEventsController:
    """Provider webhooks feeding the email suppression list. Inert unless configured."""

    @route.post("/brevo", response={200: None})
    def brevo(self, request: HttpRequest) -> tuple[int, None]:
        """Ingest Brevo transactional webhook events (single event or batch).

        404 when ``EMAIL_WEBHOOK_SECRET`` is unset (feature off). Credentials are
        ``Authorization: Bearer <secret>`` or HTTP basic auth whose password is the
        secret; anything else is 401 with no state change. Credentials are checked
        before the body is read. Any well-formed payload answers 200, including event
        kinds we ignore, so Brevo doesn't retry them.
        """
        if not settings.EMAIL_WEBHOOK_SECRET:
            raise Http404
        if not email_events.is_authorized(request.headers.get("Authorization", "")):
            raise HttpError(401, str(_("Invalid webhook credentials.")))
        for event in email_events.parse_brevo(email_events.load_json_body(request.body)):
            email_events.record_email_event(event)
        return 200, None
