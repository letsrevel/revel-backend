import stripe
from django.conf import settings
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from events.service.stripe_webhooks import StripeEventHandler


@api_controller("/stripe", auth=None)
class StripeWebhookController:
    @route.post("/webhook", response={200: None})
    def handle_webhook(self, request: HttpRequest) -> tuple[int, None]:
        """Process Stripe webhook events for payment processing.

        Handles payment confirmations, failures, and refunds. Verifies webhook signature for
        security. This endpoint is called by Stripe, not by clients directly.
        """
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
        if not sig_header:
            raise HttpError(400, str(_("Invalid Stripe signature")))
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)

        StripeEventHandler(event).handle()

        return 200, None
