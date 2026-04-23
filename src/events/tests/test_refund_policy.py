"""Unit tests for RefundPolicy Pydantic validation."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from events.utils.refund_policy import RefundPolicy, RefundPolicyTier, validate_refund_policy


class TestRefundPolicyTierValidation:
    def test_valid_tier(self) -> None:
        tier = RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("100"))
        assert tier.hours_before_event == 48
        assert tier.refund_percentage == Decimal("100")

    def test_hours_cannot_be_negative(self) -> None:
        with pytest.raises(ValidationError):
            RefundPolicyTier(hours_before_event=-1, refund_percentage=Decimal("50"))

    def test_percentage_must_be_in_0_100(self) -> None:
        with pytest.raises(ValidationError):
            RefundPolicyTier(hours_before_event=1, refund_percentage=Decimal("101"))
        with pytest.raises(ValidationError):
            RefundPolicyTier(hours_before_event=1, refund_percentage=Decimal("-1"))


class TestRefundPolicyValidation:
    def test_single_tier_policy_is_valid(self) -> None:
        policy = RefundPolicy(
            tiers=[RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100"))],
            flat_fee=Decimal("0"),
        )
        assert len(policy.tiers) == 1

    def test_multi_tier_strictly_descending_hours_is_valid(self) -> None:
        policy = RefundPolicy(
            tiers=[
                RefundPolicyTier(hours_before_event=168, refund_percentage=Decimal("100")),
                RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("50")),
                RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("25")),
            ]
        )
        assert len(policy.tiers) == 3

    def test_non_descending_hours_rejected(self) -> None:
        with pytest.raises(ValidationError, match="hours_before_event"):
            RefundPolicy(
                tiers=[
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100")),
                    RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("50")),
                ]
            )

    def test_equal_hours_rejected(self) -> None:
        with pytest.raises(ValidationError, match="hours_before_event"):
            RefundPolicy(
                tiers=[
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100")),
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("50")),
                ]
            )

    def test_increasing_refund_percentage_rejected(self) -> None:
        with pytest.raises(ValidationError, match="refund_percentage"):
            RefundPolicy(
                tiers=[
                    RefundPolicyTier(hours_before_event=48, refund_percentage=Decimal("50")),
                    RefundPolicyTier(hours_before_event=24, refund_percentage=Decimal("100")),
                ]
            )

    def test_negative_flat_fee_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RefundPolicy(
                tiers=[RefundPolicyTier(hours_before_event=0, refund_percentage=Decimal("50"))],
                flat_fee=Decimal("-1"),
            )


class TestValidateRefundPolicyHelper:
    def test_none_returns_none(self) -> None:
        assert validate_refund_policy(None) is None

    def test_valid_dict_returns_policy(self) -> None:
        result = validate_refund_policy(
            {
                "tiers": [{"hours_before_event": 24, "refund_percentage": "100"}],
                "flat_fee": "0",
            }
        )
        assert isinstance(result, RefundPolicy)
        assert result.tiers[0].hours_before_event == 24

    def test_invalid_dict_raises(self) -> None:
        with pytest.raises(ValidationError):
            validate_refund_policy(
                {
                    "tiers": [
                        {"hours_before_event": 24, "refund_percentage": "50"},
                        {"hours_before_event": 48, "refund_percentage": "100"},
                    ],
                    "flat_fee": "0",
                }
            )
