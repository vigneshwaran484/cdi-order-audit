"""Load the section 5 benchmark master data.

Run:  uv run python -m app.seed      (drops and recreates all tables!)

All parties, GSTINs and logins here are invented dummy data.
"""

from datetime import date
from decimal import Decimal as D

from sqlalchemy.orm import Session

from app.db import Base, SessionLocal, engine
from app.models import (
    Customer,
    Item,
    ItemUnitConversion,
    PackLine,
    Recipe,
    RecipeLine,
    Setting,
    Supplier,
    Unit,
    User,
)
from app.security import hash_password
from app.stock import post_move

OPENING_DATE = date(2026, 11, 1)
DEMO_PASSWORD = "demo123"


def seed(db: Session) -> None:
    # --- settings
    for key, value in {
        "company_name": "Sri Annapoorna Snacks (demo)",
        "company_gstin": "33AAAAA0000A1Z5",
        "company_state_code": "33",           # Tamil Nadu
        "company_address": "SIDCO Industrial Estate, Tiruchirappalli, Tamil Nadu",
        "variance_tolerance_pct": "3",
        "invoice_prefix": "INV",
    }.items():
        db.add(Setting(key=key, value=value))

    # --- units
    db.add_all([
        Unit(code="g", name="gram", dimension="mass", to_base=D("1")),
        Unit(code="kg", name="kilogram", dimension="mass", to_base=D("1000")),
        Unit(code="ml", name="millilitre", dimension="volume", to_base=D("1")),
        Unit(code="l", name="litre", dimension="volume", to_base=D("1000")),
        Unit(code="pc", name="piece", dimension="count", to_base=D("1")),
        Unit(code="pkt", name="packet", dimension="count", to_base=D("1")),
        Unit(code="ctn", name="carton", dimension="pack", to_base=D("1")),
    ])

    # --- users (one per role, plus two salesmen for the benchmark)
    pw = hash_password(DEMO_PASSWORD)
    users = {
        "admin": User(username="admin", full_name="Factory Admin", role="ADMIN", password_hash=pw),
        "production": User(username="production", full_name="Production Supervisor", role="PRODUCTION", password_hash=pw),
        "salesman_a": User(username="salesman_a", full_name="Salesman A", role="SALESMAN", password_hash=pw),
        "salesman_b": User(username="salesman_b", full_name="Salesman B", role="SALESMAN", password_hash=pw),
        "auditor": User(username="auditor", full_name="External Auditor", role="AUDITOR", password_hash=pw),
        "owner": User(username="owner", full_name="Owner", role="OWNER", password_hash=pw),
    }
    db.add_all(users.values())
    db.flush()

    # --- raw and packaging items
    flour = Item(code="RM-FLOUR", name="Flour", kind="RAW", stock_unit="kg", reorder_level=D("50"))
    oil = Item(code="RM-OIL", name="Cooking oil", kind="RAW", stock_unit="l", reorder_level=D("20"))
    spice = Item(code="RM-SPICE", name="Salt and spice mix", kind="RAW", stock_unit="kg", reorder_level=D("3"))
    pouch = Item(code="PK-POUCH-200", name="200 g pouch", kind="PACKAGING", stock_unit="pc", reorder_level=D("300"))
    carton = Item(code="PK-CARTON-20", name="Carton (holds 20 packets)", kind="PACKAGING", stock_unit="pc", reorder_level=D("20"))
    db.add_all([flour, oil, spice, pouch, carton])
    db.flush()

    # --- recipe: one batch of murukku = 40 kg of finished product
    # Assumption: the BOM gives 200 x 200 g packets per batch, so one batch
    # yields 40,000 g of packed product. Pack sizes share this recipe.
    recipe = Recipe(name="Murukku", output_g=D("40000"))
    recipe.lines = [
        RecipeLine(item_id=flour.id, qty=D("40"), unit="kg"),
        RecipeLine(item_id=oil.id, qty=D("10"), unit="l"),
        RecipeLine(item_id=spice.id, qty=D("1.2"), unit="kg"),
    ]
    db.add(recipe)
    db.flush()

    # --- finished product: Murukku 200 g
    murukku = Item(
        code="FG-MURUKKU-200",
        name="Murukku 200 g",
        kind="FINISHED",
        stock_unit="pkt",
        hsn_code="21069099",        # namkeen / ready-to-eat snacks
        gst_rate=D("5"),            # given by the problem statement
        sell_price=D("50.00"),
        recipe_id=recipe.id,
        net_weight_g=D("200"),
        reorder_level=D("100"),
    )
    db.add(murukku)
    db.flush()
    # BOM per batch: 200 pouches and 10 cartons for 200 packets
    murukku.pack_lines = [
        PackLine(item_id=pouch.id, qty_per_packet=D("1")),
        PackLine(item_id=carton.id, qty_per_packet=D("0.05")),  # 1 carton / 20 packets
    ]
    db.add(ItemUnitConversion(item_id=murukku.id, unit_code="ctn", factor=D("20")))

    # --- parties
    db.add_all([
        Customer(name="Retailer A", kind="RETAILER", gstin="33BBBBB1111B1Z5",
                 state_code="33", address="Main Bazaar, Tiruchirappalli, Tamil Nadu",
                 salesman_id=users["salesman_a"].id),
        Customer(name="Distributor B", kind="DISTRIBUTOR", gstin="29CCCCC2222C1Z5",
                 state_code="29", address="Avenue Road, Bengaluru, Karnataka",
                 salesman_id=users["salesman_b"].id),
        Supplier(name="Cauvery Flour Mills (demo)", gstin="33DDDDD3333D1Z5", state_code="33"),
    ])

    # --- opening stock, posted as ledger rows (never a stored balance)
    # Finished goods opening cost: assumed at standard material cost per packet
    # at opening rates = (40*42 + 10*140 + 1.2*200 + 200*2.5 + 10*15) / 200 = 19.85
    opening = [
        (flour, D("100"), D("42")),
        (oil, D("30"), D("140")),
        (spice, D("5"), D("200")),
        (pouch, D("500"), D("2.50")),
        (carton, D("30"), D("15")),
        (murukku, D("50"), D("19.85")),
    ]
    for item, qty, rate in opening:
        post_move(db, item_id=item.id, qty=qty, unit_cost=rate,
                  move_type="OPENING", source_type="OPENING", source_id=None,
                  move_date=OPENING_DATE, note="Opening balance",
                  user_id=users["admin"].id)


def reset_and_seed() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed(db)
        db.commit()


if __name__ == "__main__":
    reset_and_seed()
    print("Seeded benchmark master data.")
