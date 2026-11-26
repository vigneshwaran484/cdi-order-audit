"""Section 5 benchmark, steps 8-9: overheads, cost per packet, monthly reports.

Hand calculation of the run's material cost (weighted average):
  flour  40 kg  x 43.125 (=(100x42 + 60x45)/160)   = 1,725.00
  oil    10.5 L x 140                              = 1,470.00
  spice  1.2 kg x 200                              =   240.00
  pouch  194 pc x 2.50 (188 good + 6 damaged)      =   485.00
  carton 9 pc   x 15                               =   135.00
                                         material  = 4,055.00
  + labour 1,200 + gas/power 600                   = 5,855.00
  / 188 good packets                               = 31.14 per packet
  margin at Rs 50 = 18.86 (37.72%)
"""

from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from app.exports import amount_in_words, to_csv, to_pdf, to_xlsx
from app.models import ProductionRun
from app.services import reports
from app.services.costing import add_overhead, product_costs, run_costs
from tests.test_sales_benchmark import people, run_benchmark_steps_1_to_7  # noqa: F401

NOV_1, NOV_30 = date(2026, 11, 1), date(2026, 11, 30)


def _with_overheads(db, items, supplier, people):
    order_a, invoice_a, order_b = run_benchmark_steps_1_to_7(db, items, supplier, people)
    run_id = db.scalar(select(ProductionRun.id))
    add_overhead(db, category="LABOUR", amount=D("1200"), month=NOV_1, run_id=run_id)
    add_overhead(db, category="POWER_GAS", amount=D("600"), month=NOV_1, run_id=run_id)
    return run_id


def test_cost_per_packet(db, items, supplier, people):
    _with_overheads(db, items, supplier, people)
    (rc,) = run_costs(db, NOV_1, NOV_30)
    assert rc.material == D("4055.00")
    assert rc.direct_overhead == D("1800.00")
    assert rc.material_per_packet == D("21.57")
    assert rc.per_packet == D("31.14")
    (pc,) = product_costs(db, NOV_1, NOV_30)
    assert pc.margin == D("18.86")
    assert pc.margin_pct == D("37.72")


def test_monthly_pool_is_shared_by_kg(db, items, supplier, people):
    _with_overheads(db, items, supplier, people)
    add_overhead(db, category="OTHER", amount=D("376"), month=NOV_1, note="Electricity bill")
    (rc,) = run_costs(db, NOV_1, NOV_30)
    assert rc.pool_overhead == D("376.00")      # only one run this month -> all of it
    assert rc.per_packet == D("33.14")          # (5855 + 376) / 188


def test_stock_summary_reconciles(db, items, supplier, people):
    run_benchmark_steps_1_to_7(db, items, supplier, people)
    r = reports.stock_summary(db, NOV_1, NOV_30)
    by = {row["code"]: row for row in r.rows}
    flour = by["RM-FLOUR"]
    assert (flour["opening"], flour["receipts"], flour["issues"], flour["closing"]) == (
        D("100"), D("60"), D("40"), D("120"))
    fg = by["FG-MURUKKU-200"]
    assert (fg["opening"], fg["receipts"], fg["issues"], fg["closing"]) == (
        D("50"), D("188"), D("150"), D("88"))
    assert by["PK-POUCH-200"]["closing"] == D("306")
    assert by["RM-OIL"]["closing"] == D("19.5")


def test_stock_ledger_links_every_move_to_its_document(db, items, supplier, people):
    run_benchmark_steps_1_to_7(db, items, supplier, people)
    r = reports.stock_ledger(db, NOV_1, NOV_30, item_id=items["FG-MURUKKU-200"].id)
    sources = [row["source"] for row in r.rows]
    assert sources[0] == "Balance b/f"
    assert sources[1] == "Opening balance"
    assert sources[2].startswith("Production run FG-MURUKKU-200-20261118")
    assert sources[3] == "Invoice INV/2026-27/0001"
    assert r.rows[-1]["balance"] == D("88")


def test_gst_summary_maps_to_3b(db, items, supplier, people):
    run_benchmark_steps_1_to_7(db, items, supplier, people)
    r = reports.gst_summary(db, NOV_1, NOV_30)
    net = r.rows[-1]
    assert net["section"] == "GSTR-3B 3.1(a)"
    assert (net["taxable"], net["cgst"], net["sgst"], net["igst"]) == (
        D("7500.00"), D("187.50"), D("187.50"), D("0"))


def test_outstanding_with_ageing(db, items, supplier, people):
    run_benchmark_steps_1_to_7(db, items, supplier, people)
    r = reports.outstanding_ageing(db, NOV_1, NOV_30)
    retailer = next(row for row in r.rows if row["customer"] == "Retailer A")
    assert retailer["total"] == D("3875.00")
    assert retailer["d0_30"] == D("3875.00")


def test_production_report_flags_yield_and_oil(db, items, supplier, people):
    run_benchmark_steps_1_to_7(db, items, supplier, people)
    rows = reports.production_report(db, NOV_1, NOV_30).rows
    by = {row["line"]: row for row in rows}
    assert by["YIELD: Murukku 200 g"]["variance"] == D("-6.00")
    assert by["YIELD: Murukku 200 g"]["flag"] == "FLAG"
    assert by["Cooking oil"]["variance"] == D("5.00")
    assert by["Cooking oil"]["flag"] == "FLAG"
    assert by["Flour"]["flag"] == ""


def test_every_report_exports(db, items, supplier, people):
    _with_overheads(db, items, supplier, people)
    for key, fn in reports.REPORTS.items():
        r = fn(db, NOV_1, NOV_30)
        assert to_csv(r).startswith(b"\xef\xbb\xbf"), key
        assert to_xlsx(r)[:2] == b"PK", key          # xlsx is a zip
        assert to_pdf(r, "Demo")[:4] == b"%PDF", key


def test_amount_in_words():
    assert amount_in_words(D("7875.00")) == "Rupees Seven Thousand Eight Hundred Seventy Five Only"
    assert amount_in_words(D("187.50")) == "Rupees One Hundred Eighty Seven and Fifty Paise Only"
    assert amount_in_words(D("125000")) == "Rupees One Lakh Twenty Five Thousand Only"
