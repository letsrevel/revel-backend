from decimal import Decimal

from decouple import Csv, config

# Platform settlement currency — used as the default for ticket tiers, payments,
# exchange rate base, and referral payouts.
DEFAULT_CURRENCY = config("DEFAULT_CURRENCY", "EUR")
DEFAULT_PLATFORM_FEE_PERCENT = config("DEFAULT_PLATFORM_FEE_PERCENT", cast=Decimal, default="1.50")
DEFAULT_PLATFORM_FEE_FIXED = config("DEFAULT_PLATFORM_FEE_FIXED", cast=Decimal, default="0.25")
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="sk_test_...")
STRIPE_PUBLISHABLE_KEY = config("STRIPE_PUBLISHABLE_KEY", default="pk_test_...")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="whsec_...")
# CSV of whsec_* signing secrets — one per Stripe webhook endpoint pointing at
# /api/stripe/webhook. A two-endpoint setup (platform "Your account" + Connect
# "Connected accounts") needs both secrets here; verify_webhook tries each in
# order. Falls back to the legacy single STRIPE_WEBHOOK_SECRET so existing
# deploys keep working unchanged until the second endpoint exists.
_webhook_secrets: list[str] = config("STRIPE_WEBHOOK_SECRETS", default="", cast=Csv())
STRIPE_WEBHOOK_SECRETS: list[str] = _webhook_secrets or [STRIPE_WEBHOOK_SECRET]
# Pin the Stripe API version for outbound calls and provisioned webhook
# endpoints so a `uv sync` that bumps the stripe SDK doesn't silently change
# response shapes. NOTE: the pin is *applied* (stripe.api_version = ...) in
# Phase 2 — adding the setting first is harmless.
STRIPE_API_VERSION = config("STRIPE_API_VERSION", default="2026-03-25.dahlia")
# StripeWebhookEvent rows older than this are pruned. Stripe retries failed
# deliveries for at most 3 days, so pruned event ids can never be
# legitimately redelivered.
STRIPE_WEBHOOK_EVENT_RETENTION_DAYS = config("STRIPE_WEBHOOK_EVENT_RETENTION_DAYS", cast=int, default=90)
STRIPE_ACCOUNT = config("STRIPE_ACCOUNT", default="test_...")
# Note: minimum 30 minutes
PAYMENT_DEFAULT_EXPIRY_MINUTES = config("PAYMENT_DEFAULT_EXPIRY_MINUTES", cast=int, default=45)
# Short hold for a checkout reservation that has not yet reached the Stripe
# session-create step (#632). Reserve stamps Payment.expires_at = now + this;
# the session endpoint bumps it to PAYMENT_DEFAULT_EXPIRY_MINUTES once Stripe
# succeeds. Kept short so abandoned/never-sessioned reserves free tier capacity
# quickly during a festival on-sale — the reserve->session gap is one machine
# round-trip, so the buyer never waits this long.
RESERVATION_HOLD_MINUTES = config("RESERVATION_HOLD_MINUTES", cast=int, default=15)
# Test Stripe Connect account ID for bootstrap data
CONNECTED_TEST_STRIPE_ID = config("CONNECTED_TEST_STRIPE_ID", default=None)
DEFAULT_REFERRAL_SHARE_PERCENT = config("DEFAULT_REFERRAL_SHARE_PERCENT", cast=Decimal, default=Decimal("15.00"))
# Minimum payout amount (in DEFAULT_CURRENCY). Payouts below this are deferred to the next period.
MINIMUM_PAYOUT_AMOUNT = config("MINIMUM_PAYOUT_AMOUNT", cast=Decimal, default=Decimal("5.00"))
