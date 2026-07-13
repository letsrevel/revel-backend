from django.conf import settings


def test_reservation_hold_minutes_default() -> None:
    assert settings.RESERVATION_HOLD_MINUTES == 15
    assert settings.RESERVATION_HOLD_MINUTES < settings.PAYMENT_DEFAULT_EXPIRY_MINUTES
