import pytest
from django.core.exceptions import ValidationError

from accounts.models import RevelUser
from accounts.validators import validate_phone_number


def test_validate_phone_number_not_string() -> None:
    with pytest.raises(ValidationError, match="Phone number must be a string."):
        validate_phone_number(123)  # type: ignore[arg-type]


def test_validate_phone_number_invalid() -> None:
    with pytest.raises(ValidationError, match="Number format is incorrect."):
        validate_phone_number("123")


def test_validate_phone_number_valid() -> None:
    validate_phone_number("+39328125621")


def test_validate_phone_number_none() -> None:
    validate_phone_number(None)


@pytest.mark.django_db
def test_phone_number_field() -> None:
    number_none_user = RevelUser.objects.create_user(
        username="test_no_number",
        password="<PASSWORD>",
    )
    assert number_none_user.phone_number is None

    actual_number_user = RevelUser.objects.create(
        username="test_number",
        phone_number="+39 328 (125)62-1",
        password="<PASSWORD>",
    )
    assert actual_number_user.phone_number == "+39328125621"
