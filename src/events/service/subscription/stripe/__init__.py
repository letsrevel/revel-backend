"""Stripe half of the membership-subscription service.

Everything in here talks to Stripe Connect (direct charges) on behalf of the
local state machine in the parent package: ``payloads``/``base`` are the shared
low-level helpers, ``checkout`` owns the hosted-Checkout and subscription-mutation
flows, ``fees`` the platform ``application_fee_percent``, ``plan_change`` the
upgrade/downgrade choreography, ``sync``/``dispatch`` the webhook-driven
write-back onto local rows.

The package is deliberately named ``stripe``: inside it, ``import stripe`` still
resolves to the SDK (absolute imports), so read ``stripe.Subscription`` as the
SDK and ``subscription.stripe.<module>`` as ours.
"""
