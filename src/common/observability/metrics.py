"""Custom Prometheus business metrics.

These live on the default ``prometheus_client`` registry, which is what
``django_prometheus`` exposes at ``/metrics`` (see ``revel/urls.py``), so a
counter defined here is scraped with no extra wiring.

Only define a metric here when *someone is going to alert on it*. A metric is
the durable, actively-noticed half of an incident signal; the matching
``logger.error`` line carries the identifiers needed to actually investigate —
metric labels must stay low-cardinality, so never a session id or a user id.

Per-worker values: production runs gunicorn with several workers and no
``PROMETHEUS_MULTIPROC_DIR``, so each worker keeps its own copy of these
counters and a scrape reaches one of them. Counters here are therefore reliable
for *"did this ever happen"* alerting (``increase(...) > 0``) — the incremented
worker keeps its non-zero value and is eventually scraped — but not for exact
rates. Every metric defined here must be alert-on-any-occurrence shaped.
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
