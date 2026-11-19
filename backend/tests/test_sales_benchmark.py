"""Section 5 benchmark, steps 4-7: orders, GST invoice, dispatch, payment.

Expected results are the ones in the problem statement's results table.
"""

from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import AuditLog, Customer, StockMove, User
from app.services.sales import (
    OrderLineIn,
    SalesError,
    approve_order,
    cancel_invoice,
    create_invoice,
    customer_outstanding,
    dispatch_invoice,
    edit_order,
    record_payment,
    stock_check,
    submit_order,
    tax_preview,
)
from app.stock import InsufficientStock, balance
from tests.test_production_benchmark import do_benchmark_steps_1_to_3

DAY = date(2026, 11, 18)


@pytest.fixture
def people(db):
    users = {u.username: u for u in db.scalars(select(User))}
    customers = {c.name: c for c in db.scalars(select(Customer))}
    return users, customers


def place(db, users, customers, salesman, customer, qty, ref, items):
    return submit_order(
        db, salesman_id=users[salesman].id, customer_id=customers[customer].id,
        client_ref=ref, order_date=DAY,
        lines=[OrderLineIn(items["FG-MURUKKU-200"].id, D(qty))],
    )


def run_benchmark_steps_1_to_7(db, items, supplier, people):
    users, customers = people
    admin = users["admin"].id
    do_benchmark_steps_1_to_3(db, items, supplier)

    # Step 4: Salesman A orders 150 packets for Retailer A (Tamil Nadu)
    order_a = place(db, users, customers, "salesman_a", "Retailer A", "150", "A-0001", items)
    # Step 5: approve, invoice (CGST + SGST), dispatch
    approve_order(db, order_id=order_a.id, user_id=admin)
    invoice_a = create_invoice(db, order_id=order_a.id, invoice_date=DAY, user_id=admin)
    dispatch_invoice(db, invoice_id=invoice_a.id, dispatch_date=DAY, user_id=admin)
    # Step 6: Salesman B orders 120 packets for Distributor B (Karnataka)
    order_b = place(db, users, customers, "salesman_b", "Distributor B", "120", "B-0001", items)
    # Step 7: Rs 4,000 from Retailer A
    record_payment(db, customer_id=customers["Retailer A"].id, amount=D("4000"),
                   paid_on=DAY, mode="UPI", invoice_id=invoice_a.id, user_id=admin)
    return order_a, invoice_a, order_b


def test_retailer_a_invoice_is_intra_state(db, items, supplier, people):
    _, inv, _ = run_benchmark_steps_1_to_7(db, items, supplier, people)
    assert inv.number == "INV/2026-27/0001"
    assert inv.place_of_supply == "33"
    assert inv.taxable == D("7500.00")
    assert inv.cgst == D("187.50")
    assert inv.sgst == D("187.50")
    assert inv.igst == D("0.00")
    assert inv.total == D("7875.00")
    assert inv.lines[0].hsn_code == "21069099"


def test_dispatch_reduces_finished_goods_to_88(db, items, supplier, people):
    _, inv, _ = run_benchmark_steps_1_to_7(db, items, supplier, people)
    assert balance(db, items["FG-MURUKKU-200"].id) == D("88")  # 238 - 150
    move = db.scalar(select(StockMove).where(StockMove.move_type == "DISPATCH"))
    assert move.source_type == "INVOICE" and move.source_id == inv.id


def test_distributor_b_shortage_of_32_and_igst(db, items, supplier, people):
    users, _ = people
    _, _, order_b = run_benchmark_steps_1_to_7(db, items, supplier, people)

    [line] = stock_check(db, order_b)
    assert line.available == D("88")
    assert line.shortage == D("32")

    with pytest.raises(InsufficientStock) as err:
        approve_order(db, order_id=order_b.id, user_id=users["admin"].id)
    assert err.value.shortage == D("32")
    assert order_b.status == "SUBMITTED"   # still waiting, nothing changed

    # Karnataka is another state, so IGST would apply: 120 x 50 x 5% = 300
    preview = tax_preview(db, order_b)
    assert (preview.cgst, preview.sgst, preview.igst) == (D("0.00"), D("0.00"), D("300.00"))


def test_retailer_a_outstanding_after_payment(db, items, supplier, people):
    _, customers = people
    run_benchmark_steps_1_to_7(db, items, supplier, people)
    assert customer_outstanding(db, customers["Retailer A"].id) == D("3875.00")


def test_approved_but_undispatched_orders_count_as_committed(db, items, supplier, people):
    users, customers = people
    do_benchmark_steps_1_to_3(db, items, supplier)
    a = place(db, users, customers, "salesman_a", "Retailer A", "150", "A-1", items)
    approve_order(db, order_id=a.id, user_id=users["admin"].id)   # not yet dispatched
    b = place(db, users, customers, "salesman_b", "Distributor B", "120", "B-1", items)
    [line] = stock_check(db, b)
    assert (line.on_hand, line.committed, line.shortage) == (D("238"), D("150"), D("32"))


def test_admin_can_cut_the_order_to_what_is_available(db, items, supplier, people):
    users, _ = people
    _, _, order_b = run_benchmark_steps_1_to_7(db, items, supplier, people)
    admin = users["admin"].id
    edit_order(db, order_id=order_b.id, user_id=admin,
               lines=[OrderLineIn(items["FG-MURUKKU-200"].id, D("88"))])
    approve_order(db, order_id=order_b.id, user_id=admin)
    inv = create_invoice(db, order_id=order_b.id, invoice_date=DAY, user_id=admin)
    assert inv.number == "INV/2026-27/0002"
    assert inv.igst == D("220.00") and inv.cgst == D("0.00")   # 88 x 50 x 5%
    edit = db.scalar(select(AuditLog).where(AuditLog.entity == "order", AuditLog.action == "EDIT"))
    assert D(edit.before["lines"][0]["qty"]) == D("120")
    assert D(edit.after["lines"][0]["qty"]) == D("88")


def test_offline_resend_does_not_duplicate(db, items, people):
    users, customers = people
    first = place(db, users, customers, "salesman_a", "Retailer A", "10", "phone-uuid-1", items)
    again = place(db, users, customers, "salesman_a", "Retailer A", "10", "phone-uuid-1", items)
    assert first.id == again.id


def test_cartons_convert_to_packets(db, items, people):
    users, customers = people
    order = submit_order(
        db, salesman_id=users["salesman_a"].id, customer_id=customers["Retailer A"].id,
        client_ref="ctn-1", order_date=DAY,
        lines=[OrderLineIn(items["FG-MURUKKU-200"].id, D("2"), unit="ctn")],
    )
    assert order.lines[0].qty == D("40")


def test_salesman_cannot_set_price_or_use_others_customer(db, items, people):
    users, customers = people
    with pytest.raises(SalesError):
        submit_order(db, salesman_id=users["salesman_a"].id,
                     customer_id=customers["Retailer A"].id, client_ref="x1", order_date=DAY,
                     lines=[OrderLineIn(items["FG-MURUKKU-200"].id, D("5"), rate=D("1"))])
    with pytest.raises(SalesError):
        place(db, users, customers, "salesman_a", "Distributor B", "5", "x2", items)


def test_cancel_uses_credit_note_and_returns_stock(db, items, supplier, people):
    users, customers = people
    _, inv, _ = run_benchmark_steps_1_to_7(db, items, supplier, people)
    # A cancelled invoice can't keep a payment against it in this flow, so
    # cancel a fresh unpaid one instead: dispatch 10 more, then cancel.
    admin = users["admin"].id
    o = place(db, users, customers, "salesman_a", "Retailer A", "10", "A-2", items)
    approve_order(db, order_id=o.id, user_id=admin)
    inv2 = create_invoice(db, order_id=o.id, invoice_date=DAY, user_id=admin)
    dispatch_invoice(db, invoice_id=inv2.id, dispatch_date=DAY, user_id=admin)
    assert balance(db, items["FG-MURUKKU-200"].id) == D("78")

    note = cancel_invoice(db, invoice_id=inv2.id, reason="Wrong customer",
                          note_date=DAY, user_id=admin)
    assert note.number == "CN/2026-27/0001"
    assert note.total == inv2.total == D("525.00")
    assert inv2.status == "CANCELLED"
    assert balance(db, items["FG-MURUKKU-200"].id) == D("88")   # goods back
    assert customer_outstanding(db, customers["Retailer A"].id) == D("3875.00")


def test_payment_cannot_exceed_invoice_due(db, items, supplier, people):
    users, customers = people
    _, inv, _ = run_benchmark_steps_1_to_7(db, items, supplier, people)
    with pytest.raises(SalesError):
        record_payment(db, customer_id=customers["Retailer A"].id, amount=D("3875.01"),
                       paid_on=DAY, mode="CASH", invoice_id=inv.id, user_id=users["admin"].id)
