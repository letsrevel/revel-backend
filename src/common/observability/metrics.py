"""Custom Prometheus business metrics.

These live on the default ``prometheus_client`` registry, which is what
``django_prometheus`` exposes at ``/metrics`` (see ``revel/urls.py``), so a
counter defined here is scraped with no extra wiring.

Only define a metric here when *someone is going to alert on it*. A metric is
the durable, actively-noticed half of an incident signal; the matching
``logger.error`` line carries the identifiers needed to actually investigate —
metric labels must stay low-cardinality, so never a session id or a user id.

Presence, not rate: web workers share one registry (``PROMETHEUS_MULTIPROC_DIR``,
#757), but the metric files are wiped whenever the web container restarts, and
increments made inside Celery tasks are never scraped (Celery exports no metrics).
Every metric defined here must therefore be alert-on-any-occurrence shaped
(``increase(...) > 0``). Pre-create every child of a labelled counter (see
``events.service.stripe_incidents``): a series born at 1 on its first increment
never registers as an increase.
"""

from prometheus_client import Counter

# One occurrence is an incident, not a rate: Stripe's session total disagreed
# with sum(Payment.amount). At the "webhook" call site the buyer has already
# been charged; at "preflight" no session exists yet and nobody has been charged.
STRIPE_SESSION_TOTAL_MISMATCH = Counter(
    "revel_stripe_session_total_mismatch",
    "Stripe session total disagreed with the recorded Payment total (money-correctness breach).",
    ["call_site"],
)

# A paid checkout session arrived with no Payment rows to confirm: money was
# captured that we hold no record for, and the handler can only return 200.
STRIPE_SESSION_PAID_WITHOUT_PAYMENTS = Counter(
    "revel_stripe_session_paid_without_payments",
    "A Stripe session with a non-zero amount_total was confirmed with no matching Payment rows.",
)

# The automatic refund of such a session (#1489) could not be queued after commit: nothing else refunds the
# buyer, so the refund must be issued by hand. Incremented in the web process, so it is scraped; a failure
# inside the Celery refund task is not (it alerts through Loki on the ``orphaned_checkout_refund_failed`` log).
ORPHANED_CHECKOUT_REFUND_DISPATCH_FAILED = Counter(
    "revel_orphaned_checkout_refund_dispatch_failed",
    "The automatic refund of a paid Stripe session with no Payment rows could not be queued.",
)

# A membership invoice was paid against a locally-terminal subscription: Stripe
# billed a member whose access we already revoked, so the money needs refunding
# by hand. The local row is deliberately frozen, so nothing self-heals.
SUBSCRIPTION_PAID_WHILE_TERMINAL = Counter(
    "revel_subscription_paid_while_terminal",
    "A Stripe invoice was paid against a CANCELLED/EXPIRED membership subscription.",
)

# The PaymentIntent behind a membership invoice could not be resolved, so the
# ledger row cannot be matched by a later charge.refunded. ONLINE membership
# payments have no manual refund path, so this is unrecoverable without repair.
SUBSCRIPTION_PAYMENT_INTENT_UNRESOLVED = Counter(
    "revel_subscription_payment_intent_unresolved",
    "A MembershipPayment was recorded without a resolvable Stripe PaymentIntent id.",
)

# A paid mode=subscription checkout arrived carrying metadata we cannot match to
# any local subscription row: money was captured with nothing to attach it to.
SUBSCRIPTION_CHECKOUT_WITHOUT_ROW = Counter(
    "revel_subscription_checkout_without_row",
    "A completed subscription Checkout Session matched no local MembershipSubscription.",
)

# An invoice was paid against a locally PAUSED subscription: staff paused the
# member but an open pre-pause invoice still settled (Smart Retry, hosted invoice
# page). We keep the money and the pause — Stripe still reports pause_collection,
# and staff intent wins — so a human has to resume or refund.
SUBSCRIPTION_PAID_WHILE_PAUSED = Counter(
    "revel_subscription_paid_while_paused",
    "A Stripe invoice was paid against a PAUSED membership subscription.",
)

# A subscription Checkout Session completed against a row that had already gone
# terminal (immediate cancel, ban, refund auto-cancel) while it was still
# payable: Stripe minted a Subscription we deliberately refuse to link, so the
# money needs refunding by hand even though the Stripe side is cancelled.
SUBSCRIPTION_CHECKOUT_WHILE_TERMINAL = Counter(
    "revel_subscription_checkout_while_terminal",
    "A subscription Checkout Session completed against a CANCELLED/EXPIRED membership subscription.",
)

# An invoice was paid for a hard-blacklisted user who has no OrganizationMember
# row: a payment in flight while staff banned them. We record the money but must
# not mint an ACTIVE member (that would un-ban them), so the sub is owed a manual
# refund/cancel and nothing self-heals.
SUBSCRIPTION_PAID_WHILE_BLACKLISTED = Counter(
    "revel_subscription_paid_while_blacklisted",
    "A Stripe invoice was paid for a hard-blacklisted user with no membership row.",
)

# The nightly stale-PENDING sweep found a day-old row whose Checkout Session
# Stripe reports as complete: the member paid, but checkout.session.completed
# never linked the Stripe Subscription, so there is no membership and no ledger
# entry. The row is deliberately kept (deleting it makes the money
# undiscoverable) and a human has to link or refund it.
SUBSCRIPTION_CHECKOUT_PAID_BUT_UNLINKED = Counter(
    "revel_subscription_checkout_paid_but_unlinked",
    "A completed subscription Checkout Session was still unlinked a day later.",
)
