"""The whole section 5 benchmark driven through the web app, role by role,
the same clicks as the demo video. Also checks role-based access."""

import re
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.main import app
from app.models import Customer, Invoice, Item, Order, ProductionRun, Supplier
from app.seed import seed
from app.stock import balance

RUN_DAY = "2026-11-18"


@pytest.fixture
def web():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as s:
        seed(s)
        s.commit()
    yield
    engine.dispose()


def login(username: str) -> TestClient:
    c = TestClient(app)
    r = c.post("/login", data={"username": username, "password": "demo123"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def ids():
    with SessionLocal() as s:
        items = {i.code: i.id for i in s.scalars(select(Item))}
        custs = {c.name: c.id for c in s.scalars(select(Customer))}
        supplier = s.scalar(select(Supplier.id))
    return items, custs, supplier


def bal(code: str) -> D:
    with SessionLocal() as s:
        return balance(s, s.scalar(select(Item.id).where(Item.code == code)))


def test_benchmark_through_the_web(web):
    items, custs, supplier = ids()

    # 1. Purchase 60 kg flour at Rs 45
    prod = login("production")
    r = prod.post("/production/purchases", data={
        "supplier_id": supplier, "bill_no": "CFM-101", "bill_date": "2026-11-17",
        "item_1": items["RM-FLOUR"], "qty_1": "60", "unit_1": "kg", "rate_1": "45"})
    assert "Purchase CFM-101 saved" in r.text
    assert bal("RM-FLOUR") == D("120") + D("40")

    # 2-3. The run: the BOM form, then the actual issues and output
    page = prod.get(f"/production/runs/new?product_id={items['FG-MURUKKU-200']}&batches=1")
    assert "200 packets expected" in page.text
    r = prod.post("/production/runs", data={
        "product_id": items["FG-MURUKKU-200"], "batches": "1", "run_date": RUN_DAY,
        "actual_output": "188",
        f"issued_{items['RM-FLOUR']}": "40", f"unit_{items['RM-FLOUR']}": "kg",
        f"issued_{items['RM-OIL']}": "10.5", f"unit_{items['RM-OIL']}": "l",
        f"issued_{items['RM-SPICE']}": "1.2", f"unit_{items['RM-SPICE']}": "kg",
        f"issued_{items['PK-POUCH-200']}": "194", f"unit_{items['PK-POUCH-200']}": "pc",
        f"wasted_{items['PK-POUCH-200']}": "6",
        f"issued_{items['PK-CARTON-20']}": "9", f"unit_{items['PK-CARTON-20']}": "pc",
    })
    assert "-6.00%" in r.text and "+5.00%" in r.text and "flagged" in r.text
    assert (bal("RM-FLOUR"), bal("RM-OIL"), bal("PK-POUCH-200"), bal("FG-MURUKKU-200")) == (
        D("120"), D("19.5"), D("306"), D("238"))

    # 4. Salesman A on the phone screen (JSON API used by the offline queue)
    sa = login("salesman_a")
    assert "New order" in sa.get("/m").text
    order = {"client_ref": "phone-A-000001", "customer_id": custs["Retailer A"],
             "order_date": RUN_DAY, "lines": [{"item_id": items["FG-MURUKKU-200"], "qty": 150}]}
    first = sa.post("/api/orders", json=order).json()
    again = sa.post("/api/orders", json=order).json()        # offline resend
    assert first["id"] == again["id"]

    # 5. Admin approves + invoices in one click, then dispatches
    admin = login("admin")
    r = admin.post(f"/admin/orders/{first['id']}/approve",
                   data={"invoice_now": "1", "invoice_date": RUN_DAY})
    assert "INV/2026-27/0001" in r.text and "7,875.00" in r.text and "187.50" in r.text
    inv_id = int(re.search(r"/admin/invoices/(\d+)/pdf", r.text).group(1))
    r = admin.post(f"/admin/invoices/{inv_id}/dispatch", data={"dispatch_date": RUN_DAY})
    assert "Dispatched" in r.text
    assert bal("FG-MURUKKU-200") == D("88")
    pdf = admin.get(f"/admin/invoices/{inv_id}/pdf")
    assert pdf.content[:4] == b"%PDF"
    share = re.search(r"https://wa\.me/\?text=[^\"]+", r.text).group(0)
    token = re.search(r"%2Fi%2F([^%\"]+)", share).group(1)
    assert TestClient(app).get(f"/i/{token}").content[:4] == b"%PDF"   # no login needed

    # 6. Salesman B: 120 packets for Distributor B -> shortage of 32 shown
    sb = login("salesman_b")
    ob = sb.post("/api/orders", json={
        "client_ref": "phone-B-000001", "customer_id": custs["Distributor B"],
        "order_date": RUN_DAY, "lines": [{"item_id": items["FG-MURUKKU-200"], "qty": 120}]}).json()
    page = admin.get(f"/admin/orders/{ob['id']}")
    assert "short by" in page.text and "32 packets" in page.text and "IGST" in page.text
    r = admin.post(f"/admin/orders/{ob['id']}/approve", data={"invoice_now": "1"})
    assert "Insufficient stock" in r.text
    with SessionLocal() as s:
        assert s.get(Order, ob["id"]).status == "SUBMITTED"

    # 7. Rs 4,000 from Retailer A
    r = admin.post("/admin/payments", data={
        "customer_id": custs["Retailer A"], "amount": "4000", "mode": "UPI",
        "invoice_id": inv_id, "paid_on": RUN_DAY})
    assert "3,875.00" in r.text

    # 8. Labour 1,200 + gas/power 600 on the run -> cost per packet
    with SessionLocal() as s:
        run_id = s.scalar(select(ProductionRun.id))
    prod.post("/production/overheads", data={"category": "LABOUR", "amount": "1200", "run_id": run_id})
    r = prod.post("/production/overheads", data={"category": "POWER_GAS", "amount": "600", "run_id": run_id})
    assert "31.14" in r.text

    # 9. The auditor pulls every report and export
    aud = login("auditor")
    for key in ("sales-register", "gst-summary", "purchase-register", "stock-summary",
                "stock-ledger", "production", "cost-per-packet", "outstanding"):
        q = f"/reports?key={key}&date_from=2026-11-01&date_to=2026-11-30"
        assert aud.get(q).status_code == 200, key
        assert aud.get(q + "&fmt=xlsx").content[:2] == b"PK", key
        assert aud.get(q + "&fmt=pdf").content[:4] == b"%PDF", key
    assert "3,875.00" in aud.get("/reports?key=outstanding&date_from=2026-11-01&date_to=2026-11-30").text


def test_roles_only_see_their_screens(web):
    assert login("auditor").get("/admin/orders").status_code == 403
    assert login("salesman_a").get("/reports").status_code == 403
    assert login("production").get("/admin/payments").status_code == 403
    assert login("admin").get("/m").status_code == 403
    # the auditor is read-only: cannot record a payment
    _, custs, _ = ids()
    r = login("auditor").post("/admin/payments", data={"customer_id": custs["Retailer A"], "amount": "1"})
    assert r.status_code == 403
    assert TestClient(app).get("/admin/orders", follow_redirects=False).headers["location"] == "/login"


def test_wrong_password_is_refused(web):
    r = TestClient(app).post("/login", data={"username": "admin", "password": "nope"})
    assert "Wrong username or password" in r.text


def test_owner_dashboard_and_masters(web):
    owner = login("owner")
    assert "Owner summary" in owner.get("/dashboard").text
    assert "Murukku 200 g" in owner.get("/masters").text
    with SessionLocal() as s:
        assert s.scalar(select(Invoice)) is None
