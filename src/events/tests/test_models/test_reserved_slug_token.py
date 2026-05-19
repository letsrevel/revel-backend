"""Tests for the ReservedSlugToken model."""

import pytest
from django.core.exceptions import ValidationError

from events.models import ReservedSlugToken

pytestmark = pytest.mark.django_db


def test_clean_lowercases_and_slugifies_token() -> None:
    entry = ReservedSlugToken(token="  Spamtoken  ", reason="seed")
    entry.save()
    entry.refresh_from_db()
    assert entry.token == "spamtoken"


def test_clean_rejects_multi_segment_token() -> None:
    entry = ReservedSlugToken(token="my org", reason="")
    with pytest.raises(ValidationError) as exc:
        entry.save()
    assert "token" in exc.value.message_dict


def test_clean_rejects_empty_token() -> None:
    entry = ReservedSlugToken(token="!!!", reason="")
    with pytest.raises(ValidationError):
        entry.save()


def test_token_is_unique() -> None:
    ReservedSlugToken.objects.create(token="uniquetest", reason="")
    with pytest.raises(ValidationError):
        ReservedSlugToken(token="UNIQUETEST", reason="").save()
