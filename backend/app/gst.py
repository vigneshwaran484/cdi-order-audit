"""GST maths, kept free of the database so it is easy to test.

Rules used:
- Place of supply = the customer's state (goods delivered to the buyer).
- Same state as the factory  -> intra-state: CGST + SGST, each half the rate.
- Different state            -> inter-state: IGST at the full rate.
- Tax is computed per invoice line and rounded to the paisa (half-up);
  invoice totals are the sum of the rounded line amounts, so the lines
  always add up exactly to the total printed on the invoice.
- Financial year runs 1 April to 31 March: 18 Nov 2026 is in '2026-27'.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

PAISA = Decimal("0.01")


def _money(x: Decimal) -> Decimal:
    return Decimal(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def fin_year(d: date) -> str:
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


@dataclass(frozen=True)
class LineTax:
    taxable: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal

    @property
    def total(self) -> Decimal:
        return self.taxable + self.cgst + self.sgst + self.igst


def line_tax(qty: Decimal, rate: Decimal, gst_rate_pct: Decimal, intra_state: bool) -> LineTax:
    taxable = _money(Decimal(qty) * Decimal(rate))
    if intra_state:
        half = _money(taxable * Decimal(gst_rate_pct) / 2 / 100)
        return LineTax(taxable, half, half, Decimal("0.00"))
    igst = _money(taxable * Decimal(gst_rate_pct) / 100)
    return LineTax(taxable, Decimal("0.00"), Decimal("0.00"), igst)


def sum_taxes(lines: list[LineTax]) -> LineTax:
    zero = Decimal("0.00")
    return LineTax(
        sum((ln.taxable for ln in lines), zero),
        sum((ln.cgst for ln in lines), zero),
        sum((ln.sgst for ln in lines), zero),
        sum((ln.igst for ln in lines), zero),
    )
