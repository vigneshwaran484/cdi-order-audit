"""Section 5 benchmark, steps 1-3: purchase, production run, output.

Expected results are the ones in the problem statement's results table.
"""

from datetime import date
from decimal import Decimal as D

import pytest

from app.services.production import IssueIn, plan, record_run, run_summary
from app.services.purchases import PurchaseLineIn, record_purchase
from app.stock import InsufficientStock, avg_cost, balance

RUN_DATE = date(2026, 11, 18)


def do_benchmark_steps_1_to_3(db, items, supplier):
    # Step 1: 60 kg flour at Rs 45/kg
    record_purchase(
        db, supplier_id=supplier.id, bill_no="CFM-101", bill_date=date(2026, 11, 17),
        lines=[PurchaseLineIn(items["RM-FLOUR"].id, D("60"), "kg", D("45"))],
    )
    # Steps 2-3: one batch; oil 0.5 L over standard; 188 good packets,
    # 6 pouches damaged (so 194 issued), 9 cartons used.
    return record_run(
        db,
        product_id=items["FG-MURUKKU-200"].id,
        run_date=RUN_DATE,
        batches=D("1"),
        actual_output=D("188"),
        issues=[
            IssueIn(items["RM-FLOUR"].id, D("40"), "kg"),
            IssueIn(items["RM-OIL"].id, D("10.5"), "l"),
            IssueIn(items["RM-SPICE"].id, D("1.2"), "kg"),
            IssueIn(items["PK-POUCH-200"].id, D("194"), "pc", wasted_qty=D("6")),
            IssueIn(items["PK-CARTON-20"].id, D("9"), "pc"),
        ],
    )


def test_closing_balances_after_run(db, items, supplier):
    do_benchmark_steps_1_to_3(db, items, supplier)

    assert balance(db, items["RM-FLOUR"].id) == D("120")      # 100 + 60 - 40
    assert balance(db, items["RM-OIL"].id) == D("19.5")       # 30 - 10.5
    assert balance(db, items["RM-SPICE"].id) == D("3.8")      # 5 - 1.2
    assert balance(db, items["PK-POUCH-200"].id) == D("306")  # 500 - 188 - 6
    assert balance(db, items["PK-CARTON-20"].id) == D("21")   # 30 - 9
    assert balance(db, items["FG-MURUKKU-200"].id) == D("238")  # 50 + 188


def test_variances_are_flagged(db, items, supplier):
    run = do_benchmark_steps_1_to_3(db, items, supplier)
    s = run_summary(db, run)
    by_item = {ln["item"]: ln for ln in s["lines"]}

    assert s["expected_output"] == D("200")
    assert s["yield_variance_pct"] == D("-6.00")
    assert s["yield_flagged"] is True

    assert by_item["Cooking oil"]["variance_pct"] == D("5.00")
    assert by_item["Cooking oil"]["flagged"] is True

    assert by_item["Flour"]["variance_pct"] == D("0.00")
    assert by_item["Flour"]["flagged"] is False

    # 194 issued vs 188 needed for the good output: damaged pouches show up
    assert by_item["200 g pouch"]["planned"] == D("188")
    assert by_item["200 g pouch"]["wasted"] == D("6")
    assert by_item["200 g pouch"]["flagged"] is True

    # 188 packets = 9 full cartons (8 packets loose): no variance
    assert by_item["Carton (holds 20 packets)"]["planned"] == D("9")
    assert by_item["Carton (holds 20 packets)"]["flagged"] is False


def test_material_cost_uses_weighted_average(db, items, supplier):
    run = do_benchmark_steps_1_to_3(db, items, supplier)
    # Flour avg after purchase: (100*42 + 60*45) / 160 = 43.125
    # 40*43.125 + 10.5*140 + 1.2*200 + 194*2.50 + 9*15
    #   = 1725 + 1470 + 240 + 485 + 135 = 4055.00
    assert run.material_cost == D("4055.00")
    assert avg_cost(db, items["RM-FLOUR"].id) == D("43.1250")


def test_every_move_traces_to_its_run(db, items, supplier):
    from sqlalchemy import select

    from app.models import StockMove

    run = do_benchmark_steps_1_to_3(db, items, supplier)
    moves = db.scalars(select(StockMove).where(StockMove.source_type == "PRODUCTION_RUN")).all()
    assert len(moves) == 6  # 5 issues + 1 output
    assert all(m.source_id == run.id for m in moves)


def test_run_refused_when_raw_material_short(db, items):
    with pytest.raises(InsufficientStock) as err:
        record_run(
            db, product_id=items["FG-MURUKKU-200"].id, run_date=RUN_DATE,
            batches=D("3"), actual_output=D("600"),
            issues=[IssueIn(items["RM-FLOUR"].id, D("120"), "kg")],
        )
    assert err.value.item_name == "Flour"
    assert err.value.shortage == D("20")


def test_500g_pack_shares_the_recipe(db, items):
    """The interview change: a 500 g pack needs no new recipe."""
    from app.models import Item, PackLine

    m200 = items["FG-MURUKKU-200"]
    m500 = Item(code="FG-MURUKKU-500", name="Murukku 500 g", kind="FINISHED",
                stock_unit="pkt", hsn_code="21069099", gst_rate=D("5"),
                sell_price=D("120"), recipe_id=m200.recipe_id, net_weight_g=D("500"))
    db.add(m500)
    db.flush()
    db.add(PackLine(product_id=m500.id, item_id=items["PK-CARTON-20"].id,
                    qty_per_packet=D("0.1")))
    db.flush()
    db.refresh(m500)

    from app.services.production import expected_output
    assert expected_output(m500, D("1")) == D("80")       # 40 kg / 500 g
    planned = {p.item_name: p.planned_qty for p in plan(db, m500, D("1"), D("80"))}
    assert planned["Flour"] == D("40")
    assert planned["Carton (holds 20 packets)"] == D("8")
