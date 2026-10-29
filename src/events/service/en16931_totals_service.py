"""EN 16931 totals for attendee invoices and credit notes, with the charged gross preserved (#1234).

Stripe has already charged a fixed gross ``G`` per line. EN 16931 derives VAT per
``(category, rate)`` group from the group's net (BT-117 = round(BT-116 x r), BR-CO-17),
and some grosses cannot be reached from any 2-dp net (24.90 at 13%: 22.03 gives 24.89,
22.04 gives 24.91). Per group this picks the net that reaches ``G`` when one exists and
otherwise books the unavoidable cent as the document's rounding amount (BT-114), so that
``total_net + total_vat + rounding_amount == total_gross`` always holds and the total
stays what the buyer paid.

Pure: no DB access. The lines go in and come out as :class:`InvoiceLineItemDict`.
"""

import typing as t
from decimal import ROUND_HALF_UP, Decimal

from events.models.attendee_invoice import InvoiceLineItemDict
from events.service.vat_service import distribute_amount_pro_rata
from events.utils.vat_category import VatCategory, derive_vat_category

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

GroupKey = tuple[str, Decimal]


class En16931Group(t.NamedTuple):
    """One ``(category, rate)`` VAT breakdown group (BG-23) of a document."""

    category: str
    rate: Decimal
    gross: Decimal
    net: Decimal
    vat: Decimal
    rounding: Decimal


class En16931Totals(t.NamedTuple):
    """The rebalanced lines and header totals of a document."""

    lines: list[InvoiceLineItemDict]
    groups: list[En16931Group]
    total_gross: Decimal
    total_net: Decimal
    total_vat: Decimal
    rounding_amount: Decimal


def _vat_of(net: Decimal, rate: Decimal) -> Decimal:
    return (net * rate / 100).quantize(CENT, rounding=ROUND_HALF_UP)


def group_net_vat(gross: Decimal, rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    """Split a group's charged gross into ``(net, vat, rounding)`` the EN 16931 way.

    A 2-dp net ``N`` with ``N + round(N x rate) == gross`` is searched among
    ``round(gross / (1 + rate))`` and its two cent-neighbours; when found, VAT is
    ``gross - N`` and the rounding is zero. Otherwise the rounded net is kept, VAT is
    ``round(N x rate)`` exactly, and the leftover cent (always +/-0.01) is the rounding.
    A 0% group is net = gross with nothing to round.
    """
    if rate == 0:
        return gross, ZERO, ZERO
    candidate = (gross * 100 / (100 + rate)).quantize(CENT, rounding=ROUND_HALF_UP)
    for net in (candidate, candidate - CENT, candidate + CENT):
        if net + _vat_of(net, rate) == gross:
            return net, gross - net, ZERO
    vat = _vat_of(candidate, rate)
    return candidate, vat, gross - (candidate + vat)


def line_group_key(line: InvoiceLineItemDict, reverse_charge: bool) -> GroupKey:
    """The ``(vat_category, rate)`` VAT breakdown group of a line.

    A line without a stored category takes the one derived from its rate and ``reverse_charge``.

    Raises:
        ValueError: If a reverse-charge (AE) line carries a non-zero rate. Nothing produces one
            (checkout writes rate 0, a DRAFT edit answers 422), so inventing a VAT for it is refused.
    """
    rate = Decimal(line["vat_rate"])
    category = line.get("vat_category") or str(derive_vat_category(rate, reverse_charge))
    if category == VatCategory.REVERSE_CHARGE and rate > 0:
        raise ValueError("reverse-charge line with non-zero VAT rate")
    return category, rate


def group_lines(lines: list[InvoiceLineItemDict], reverse_charge: bool) -> dict[GroupKey, list[int]]:
    """Index positions of ``lines`` by group, groups in order of first appearance."""
    members: dict[GroupKey, list[int]] = {}
    for index, line in enumerate(lines):
        members.setdefault(line_group_key(line, reverse_charge), []).append(index)
    return members


def spread_group_lines(net: Decimal, rounding: Decimal, grosses: list[Decimal]) -> list[tuple[Decimal, Decimal]]:
    """Spread a group's net over its lines and make every line foot.

    The net goes pro rata by gross (:func:`distribute_amount_pro_rata`), each line's VAT is its
    gross minus its net, and the group's ``rounding`` is taken off the VAT of exactly one line:
    the largest gross, first on ties. So the line nets sum to ``net``, the line VATs sum to
    ``gross - net - rounding`` (the group's VAT), and ``net + VAT == gross`` holds on every line
    but that one, which is off by exactly the rounding.

    Returns:
        One ``(net, vat)`` pair per gross, in order.
    """
    nets = distribute_amount_pro_rata(net, grosses)
    vats = [gross - line_net for gross, line_net in zip(grosses, nets, strict=True)]
    if rounding:
        vats[max(range(len(grosses)), key=lambda i: (grosses[i], -i))] -= rounding
    return list(zip(nets, vats, strict=True))


def compute_en16931_totals(lines: list[InvoiceLineItemDict], reverse_charge: bool) -> En16931Totals:
    """Regroup ``lines`` by ``(vat_category, vat_rate)`` and rebalance their net and VAT.

    Each group's net and rounding (from :func:`group_net_vat`) are spread back over its lines
    by :func:`spread_group_lines`, so the line nets of a group sum exactly to its net (BT-116),
    the line VATs to its VAT, and every printed row foots. Groups keep their order of first
    appearance (see :func:`line_group_key`).

    Returns:
        New line dicts (the inputs are not mutated) with ``net_amount`` and ``vat_amount``
        rewritten as strings and every other key, ``unit_price_gross`` included, unchanged;
        the groups; and the header totals, for which
        ``total_net + total_vat + rounding_amount == total_gross``.
    """
    out_lines = [line.copy() for line in lines]
    groups: list[En16931Group] = []
    for (category, rate), indexes in group_lines(lines, reverse_charge).items():
        grosses = [Decimal(lines[i]["unit_price_gross"]) for i in indexes]
        gross = sum(grosses, ZERO)
        net, vat, rounding = group_net_vat(gross, rate)
        groups.append(En16931Group(category, rate, gross, net, vat, rounding))
        for i, (line_net, line_vat) in zip(indexes, spread_group_lines(net, rounding, grosses), strict=True):
            out_lines[i]["net_amount"] = str(line_net)
            out_lines[i]["vat_amount"] = str(line_vat)

    return En16931Totals(
        lines=out_lines,
        groups=groups,
        total_gross=sum((g.gross for g in groups), ZERO),
        total_net=sum((g.net for g in groups), ZERO),
        total_vat=sum((g.vat for g in groups), ZERO),
        rounding_amount=sum((g.rounding for g in groups), ZERO),
    )
