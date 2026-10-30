"""EN 16931 per-rate totals with the charged gross preserved (#1234)."""

import copy
import random
from decimal import ROUND_HALF_UP, Decimal

import pytest

from events.models.attendee_invoice import InvoiceLineItemDict
from events.service.en16931_totals_service import compute_en16931_totals, group_net_vat, sum_line_amounts
from events.utils.vat_category import derive_vat_category

CENT = Decimal("0.01")
RATES = [0, 5, 7, 9, 10, 12, 13, 19, 20, 21, 22, 23, 24, 25, 27]


def _line(gross: str, rate: str, category: str | None = None, description: str = "Ticket") -> InvoiceLineItemDict:
    line = InvoiceLineItemDict(
        description=description,
        unit_price_gross=gross,
        discount_amount="0.00",
        net_amount="0.00",
        vat_amount="0.00",
        vat_rate=rate,
    )
    if category is not None:
        line["vat_category"] = category
    return line


class TestGroupNetVat:
    def test_reachable_gross_has_no_rounding(self) -> None:
        assert group_net_vat(Decimal("25.00"), Decimal("13")) == (Decimal("22.12"), Decimal("2.88"), Decimal("0.00"))

    def test_unreachable_gross_rounds_one_cent(self) -> None:
        """24.90 at 13%: 22.03 gives 24.89 and 22.04 gives 24.91, so BT-114 absorbs the cent."""
        assert group_net_vat(Decimal("24.90"), Decimal("13")) == (
            Decimal("22.04"),
            Decimal("2.87"),
            Decimal("-0.01"),
        )

    def test_zero_rate_group(self) -> None:
        assert group_net_vat(Decimal("10.00"), Decimal("0")) == (Decimal("10.00"), Decimal("0.00"), Decimal("0.00"))


class TestComputeEn16931Totals:
    def test_large_cart_rounds_at_most_one_cent_per_group(self) -> None:
        """Review focus 2: 50 x 24.90 at 13% rounds once for the group, not once per ticket."""
        totals = compute_en16931_totals([_line("24.90", "13.00") for _ in range(50)], reverse_charge=False)
        assert totals.total_gross == Decimal("1245.00")
        assert abs(totals.rounding_amount) <= CENT
        assert totals.total_net + totals.total_vat + totals.rounding_amount == totals.total_gross

    def test_mixed_cart_groups_and_line_sums(self) -> None:
        """Review focus 5: S20 / E0 / S10 lines make three groups, each with line nets summing to its N."""
        lines = [
            _line("120.00", "20.00", "S", "A"),
            _line("10.00", "0.00", "E", "B"),
            _line("33.00", "10.00", "S", "C"),
            _line("60.00", "20.00", "S", "D"),
        ]
        totals = compute_en16931_totals(lines, reverse_charge=False)
        assert [(g.category, g.rate) for g in totals.groups] == [
            ("S", Decimal("20.00")),
            ("E", Decimal("0.00")),
            ("S", Decimal("10.00")),
        ]
        for group in totals.groups:
            members = [
                line
                for line in totals.lines
                if (line["vat_category"], Decimal(line["vat_rate"])) == (group.category, group.rate)
            ]
            assert sum(Decimal(line["net_amount"]) for line in members) == group.net
            assert sum(Decimal(line["vat_amount"]) for line in members) == group.vat
        assert [line["description"] for line in totals.lines] == ["A", "B", "C", "D"]

    def test_line_gross_unchanged_and_inputs_not_mutated(self) -> None:
        lines = [_line("24.90", "13.00", "S"), _line("5.10", "13.00", "S")]
        snapshot = copy.deepcopy(lines)
        totals = compute_en16931_totals(lines, reverse_charge=False)
        assert lines == snapshot
        assert [line["unit_price_gross"] for line in totals.lines] == ["24.90", "5.10"]
        assert all(out is not src for out, src in zip(totals.lines, lines, strict=True))

    def test_every_line_foots_when_the_group_is_reachable(self) -> None:
        """Review fix 1: two 7.99 @ 10% lines (15.98 is reachable) print rows where net + VAT == gross."""
        totals = compute_en16931_totals(
            [_line("7.99", "10.00", "S"), _line("7.99", "10.00", "S")], reverse_charge=False
        )
        assert totals.rounding_amount == Decimal("0.00")
        for line in totals.lines:
            assert Decimal(line["net_amount"]) + Decimal(line["vat_amount"]) == Decimal("7.99")
        assert sum(Decimal(line["vat_amount"]) for line in totals.lines) == totals.total_vat

    def test_group_rounding_lands_on_the_largest_gross_line(self) -> None:
        """An unreachable group puts its BT-114 cent on exactly one line: the largest gross, first on ties."""
        lines = [
            _line("5.00", "13.00", "S", "small"),
            _line("9.95", "13.00", "S", "big"),
            _line("9.95", "13.00", "S", "tie"),
        ]
        totals = compute_en16931_totals(lines, reverse_charge=False)
        rounding = totals.groups[0].rounding
        assert rounding == Decimal("-0.01")  # 24.90 at 13% is unreachable
        off = [
            line
            for line in totals.lines
            if Decimal(line["net_amount"]) + Decimal(line["vat_amount"]) != Decimal(line["unit_price_gross"])
        ]
        assert [line["description"] for line in off] == ["big"]
        assert Decimal(off[0]["net_amount"]) + Decimal(off[0]["vat_amount"]) + rounding == Decimal("9.95")

    def test_reverse_charge_line_with_vat_rate_is_rejected(self) -> None:
        """Review fix 5: an AE line never carries a rate; the function refuses rather than invent a VAT."""
        with pytest.raises(ValueError, match="reverse-charge line with non-zero VAT rate"):
            compute_en16931_totals([_line("100.00", "20.00", "AE")], reverse_charge=True)

    def test_reverse_charge_lines_are_ae_zero(self) -> None:
        """A reverse-charge line has no stored category here, so the key is derived: AE at 0%."""
        totals = compute_en16931_totals([_line("100.00", "0.00")], reverse_charge=True)
        assert [(g.category, g.rate) for g in totals.groups] == [("AE", Decimal("0.00"))]
        assert totals.total_net == Decimal("100.00")
        assert totals.total_vat == Decimal("0.00")
        assert totals.rounding_amount == Decimal("0.00")
        assert totals.lines[0]["net_amount"] == "100.00"

    def test_property_invariants_seeded(self) -> None:
        rng = random.Random(1234)
        for _ in range(500):
            lines: list[InvoiceLineItemDict] = []
            for _ in range(rng.randint(1, 50)):
                rate = Decimal(rng.choice(RATES))
                gross = (Decimal(rng.randint(50, 50_000)) / 100).quantize(CENT)
                lines.append(_line(str(gross), str(rate), str(derive_vat_category(rate, reverse_charge=False))))
            totals = compute_en16931_totals(lines, reverse_charge=False)

            assert totals.total_net + totals.total_vat + totals.rounding_amount == totals.total_gross
            assert totals.total_gross == sum(Decimal(line["unit_price_gross"]) for line in lines)
            for group in totals.groups:
                assert abs(group.rounding) <= CENT
                if group.rate > 0:
                    assert group.vat == (group.net * group.rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)
                members = [
                    line
                    for line in totals.lines
                    if (line["vat_category"], Decimal(line["vat_rate"])) == (group.category, group.rate)
                ]
                assert sum(Decimal(line["net_amount"]) for line in members) == group.net
                assert sum(Decimal(line["vat_amount"]) for line in members) == group.vat
                # Review fix 1: every row foots, except at most one per group carrying that group's cent.
                off_by = [
                    Decimal(line["net_amount"]) + Decimal(line["vat_amount"]) - Decimal(line["unit_price_gross"])
                    for line in members
                ]
                non_footing = [delta for delta in off_by if delta != 0]
                assert non_footing == ([-group.rounding] if group.rounding != 0 else [])


def test_sum_line_amounts_returns_the_three_stored_sums() -> None:
    """The legacy triple every pre-EN 16931 site sums: gross, net, VAT, straight from the stored strings."""
    lines = [_line("24.90", "13.00"), _line("10.00", "0.00")]
    lines[0]["net_amount"], lines[0]["vat_amount"] = "22.04", "2.86"
    lines[1]["net_amount"] = "10.00"
    assert sum_line_amounts(lines) == (Decimal("34.90"), Decimal("32.04"), Decimal("2.86"))
    assert sum_line_amounts([]) == (Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))
