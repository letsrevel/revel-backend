"""Model tests: case-insensitive referral codes, per-referrer share override, referral applications."""

import typing as t
from decimal import Decimal

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError

from accounts.models import Referral, ReferralApplication, ReferralCode, RevelUser

pytestmark = pytest.mark.django_db


@pytest.fixture
def referrer(revel_user_factory: t.Any) -> RevelUser:
    return t.cast(RevelUser, revel_user_factory())


@pytest.fixture
def other_user(revel_user_factory: t.Any) -> RevelUser:
    return t.cast(RevelUser, revel_user_factory())


class TestReferralCodeCase:
    def test_code_is_stored_as_typed(self, referrer: RevelUser) -> None:
        code = ReferralCode.objects.create(user=referrer, code="biagio")
        code.refresh_from_db()
        assert code.code == "biagio"

    def test_code_is_unique_case_insensitively(self, referrer: RevelUser, other_user: RevelUser) -> None:
        ReferralCode.objects.create(user=referrer, code="Biagio")
        with pytest.raises(ValidationError):
            ReferralCode.objects.create(user=other_user, code="BIAGIO")

    @pytest.mark.parametrize("bad", ["ab", "has space", "bad!", "x" * 21])
    def test_code_charset_and_length_are_validated(self, referrer: RevelUser, bad: str) -> None:
        with pytest.raises(ValidationError):
            ReferralCode.objects.create(user=referrer, code=bad)

    def test_override_is_optional(self, referrer: RevelUser) -> None:
        code = ReferralCode.objects.create(user=referrer, code="plain")
        assert code.revenue_share_percent is None


class TestReferralShareSnapshot:
    def test_snapshots_code_override(self, referrer: RevelUser, other_user: RevelUser) -> None:
        code = ReferralCode.objects.create(user=referrer, code="twenty", revenue_share_percent=Decimal("20.00"))
        referral = Referral.objects.create(referral_code=code, referred_user=other_user)
        assert referral.revenue_share_percent == Decimal("20.00")

    def test_falls_back_to_setting(self, referrer: RevelUser, other_user: RevelUser) -> None:
        code = ReferralCode.objects.create(user=referrer, code="plain")
        referral = Referral.objects.create(referral_code=code, referred_user=other_user)
        assert referral.revenue_share_percent == settings.DEFAULT_REFERRAL_SHARE_PERCENT

    def test_explicit_non_default_value_wins(self, referrer: RevelUser, other_user: RevelUser) -> None:
        code = ReferralCode.objects.create(user=referrer, code="twenty", revenue_share_percent=Decimal("20.00"))
        referral = Referral.objects.create(
            referral_code=code, referred_user=other_user, revenue_share_percent=Decimal("5.00")
        )
        assert referral.revenue_share_percent == Decimal("5.00")

    def test_existing_referral_is_not_rewritten(self, referrer: RevelUser, other_user: RevelUser) -> None:
        code = ReferralCode.objects.create(user=referrer, code="plain")
        referral = Referral.objects.create(referral_code=code, referred_user=other_user)
        code.revenue_share_percent = Decimal("30.00")
        code.save()
        referral.save()
        referral.refresh_from_db()
        assert referral.revenue_share_percent == settings.DEFAULT_REFERRAL_SHARE_PERCENT


class TestReferralApplicationModel:
    def test_email_is_lowercased_and_normalized(self) -> None:
        app = ReferralApplication.objects.create(email="Foo.Bar+tag@Gmail.com", code="foobar", note="hi")
        assert app.email == "foo.bar+tag@gmail.com"
        assert app.normalized_email == "foobar@gmail.com"

    def test_public_application_requires_note(self) -> None:
        with pytest.raises(ValidationError):
            ReferralApplication.objects.create(email="a@example.com", code="abc", note="   ")

    def test_invite_may_omit_note(self) -> None:
        app = ReferralApplication.objects.create(
            email="a@example.com", code="abc", source=ReferralApplication.Source.INVITE
        )
        assert app.status == ReferralApplication.Status.PENDING
        assert app.revenue_share_percent == settings.DEFAULT_REFERRAL_SHARE_PERCENT

    def test_one_pending_application_per_normalized_email(self) -> None:
        ReferralApplication.objects.create(email="a@example.com", code="one", note="hi")
        with pytest.raises(ValidationError):
            ReferralApplication.objects.create(email="A@example.com", code="two", note="hi")

    def test_rejected_row_does_not_block_a_new_pending_one(self) -> None:
        ReferralApplication.objects.create(
            email="a@example.com", code="one", note="hi", status=ReferralApplication.Status.REJECTED
        )
        ReferralApplication.objects.create(email="a@example.com", code="two", note="hi")
        assert ReferralApplication.objects.filter(normalized_email="a@example.com").count() == 2
