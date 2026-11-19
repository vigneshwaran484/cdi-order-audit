from datetime import date
from decimal import Decimal as D

from app.gst import fin_year, line_tax, sum_taxes


def test_financial_year_starts_in_april():
    assert fin_year(date(2026, 11, 18)) == "2026-27"
    assert fin_year(date(2027, 3, 31)) == "2026-27"
    assert fin_year(date(2027, 4, 1)) == "2027-28"
    assert fin_year(date(2099, 12, 1)) == "2099-00"


def test_intra_state_splits_cgst_and_sgst():
    t = line_tax(D("150"), D("50"), D("5"), intra_state=True)
    assert (t.taxable, t.cgst, t.sgst, t.igst, t.total) == (
        D("7500.00"), D("187.50"), D("187.50"), D("0.00"), D("7875.00"))


def test_inter_state_is_igst():
    t = line_tax(D("120"), D("50"), D("5"), intra_state=False)
    assert (t.cgst, t.sgst, t.igst) == (D("0.00"), D("0.00"), D("300.00"))


def test_rounding_is_per_line_half_up_and_totals_add_up():
    # 3 x 33.33 = 99.99; 2.5% = 2.49975 -> 2.50 each half
    a = line_tax(D("3"), D("33.33"), D("5"), intra_state=True)
    assert (a.cgst, a.sgst) == (D("2.50"), D("2.50"))
    b = line_tax(D("1"), D("0.10"), D("5"), intra_state=True)   # 0.0025 -> 0.00
    total = sum_taxes([a, b])
    assert total.total == a.total + b.total == D("105.09")
