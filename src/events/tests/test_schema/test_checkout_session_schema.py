from uuid import uuid4

from events import schema


def test_batch_response_defaults() -> None:
    r = schema.BatchCheckoutResponse()  # type: ignore[call-arg]
    assert r.reservation_id is None
    assert r.requires_payment is False
    assert r.tickets == []


def test_checkout_session_response_requires_url() -> None:
    rid = uuid4()
    r = schema.BatchCheckoutResponse(reservation_id=rid, requires_payment=True)  # type: ignore[call-arg]
    assert r.requires_payment is True
    s = schema.CheckoutSessionResponse(checkout_url="https://checkout.stripe.com/x")
    assert s.checkout_url.startswith("https://")
