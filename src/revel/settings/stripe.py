from decimal import Decimal

from decouple import config

# Platform settlement currency — used as the default for ticket tiers, payments,
# exchange rate base, and referral payouts.
DEFAULT_CURRENCY = config("DEFAULT_CURRENCY", "EUR")
DEFAULT_PLATFORM_FEE_PERCENT = config("DEFAULT_PLATFORM_FEE_PERCENT", cast=Decimal, default="1.50")
DEFAULT_PLATFORM_FEE_FIXED = config("DEFAULT_PLATFORM_FEE_FIXED", cast=Decimal, default="0.25")
STRIPE_SECRET_KEY = config("STRIPE_SECRET_KEY", default="sk_test_...")
STRIPE_PUBLISHABLE_KEY = config("STRIPE_PUBLISHABLE_KEY", default="pk_test_...")
STRIPE_WEBHOOK_SECRET = config("STRIPE_WEBHOOK_SECRET", default="whsec_...")
STRIPE_ACCOUNT = config("STRIPE_ACCOUNT", default="test_...")
# Note: minimum 30 minutes
PAYMENT_DEFAULT_EXPIRY_MINUTES = config("PAYMENT_DEFAULT_EXPIRY_MINUTES", cast=int, default=45)
# Test Stripe Connect account ID for bootstrap data
CONNECTED_TEST_STRIPE_ID = config("CONNECTED_TEST_STRIPE_ID", default=None)
DEFAULT_REFERRAL_SHARE_PERCENT = config("DEFAULT_REFERRAL_SHARE_PERCENT", cast=Decimal, default=Decimal("15.00"))
# Minimum payout amount (in DEFAULT_CURRENCY). Payouts below this are deferred to the next period.
MINIMUM_PAYOUT_AMOUNT = config("MINIMUM_PAYOUT_AMOUNT", cast=Decimal, default=Decimal("5.00"))
