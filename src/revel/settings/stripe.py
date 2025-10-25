from decimal import Decimal

from decouple import config

DEFAULT_CURRENCY = config("DEFAULT_CURRENCY", "EUR")
DEFAULT_PLATFORM_FEE_PERCENT = config("DEFAULT_PLATFORM_FEE_PERCENT", cast=Decimal, default="3.00")
DEFAULT_PLATFORM_FEE_FIXED = config("DEFAULT_PLATFORM_FEE_FIXED", cast=Decimal, default="0.5")
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="sk_test_...")
STRIPE_PUBLISHABLE_KEY = config("STRIPE_PUBLISHABLE_KEY", default="pk_test_...")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="whsec_...")
# Note: minimum 30 minutes
PAYMENT_DEFAULT_EXPIRY_MINUTES = config("PAYMENT_DEFAULT_EXPIRY_MINUTES", cast=int, default=45)
# Test Stripe Connect account ID for bootstrap data
CONNECTED_TEST_STRIPE_ID = config("CONNECTED_TEST_STRIPE_ID", default=None)
