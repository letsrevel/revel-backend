import stripe
from django.conf import settings
from django.http import HttpRequest
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from events.service import stripe_service


@api_controller("/stripe", auth=None)
class StripeWebhookController:
    @route.post("/webhook", response={200: None})
    def handle_webhook(self, request: HttpRequest) -> tuple[int, None]:
        """Handle incoming Stripe webhooks."""
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
        if not sig_header:
            raise HttpError(400, "Invalid Stripe signature")
        event = stripe.Webhook.construct_event(payload, sig_header, settings.STRIPE_WEBHOOK_SECRET)

        stripe_service.StripeEventHandler(event).handle()

        return 200, None
