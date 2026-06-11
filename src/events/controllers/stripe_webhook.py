from django.http import HttpRequest
from ninja_extra import api_controller, route

from events.exceptions import InvalidStripeWebhookSignatureError
from events.service import stripe_webhooks


@api_controller("/stripe", auth=None)
class StripeWebhookController:
    @route.post("/webhook", response={200: None})
    def handle_webhook(self, request: HttpRequest) -> tuple[int, None]:
        """Process Stripe webhook events for payment processing.

        Signature validation fails closed (403 via the events exception
        handler) when no configured secret matches. The body is consumed as
        raw bytes — Stripe signs the verbatim payload, so any re-parse before
        verification would break the HMAC.
        """
        payload = request.body
        sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
        if not sig_header:
            raise InvalidStripeWebhookSignatureError()
        event = stripe_webhooks.verify_webhook(payload, sig_header)
        stripe_webhooks.handle_event(event)
        return 200, None
