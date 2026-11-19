"""Database schema.

Design rules (from the problem statement, section 7):
- Stock is NEVER stored as an editable number. Every change is a row in
  `stock_moves`; a balance is SUM(qty) over that table.
- Every stock move points back to its source document (source_type + source_id).
- Money and quantities are NUMERIC (Python Decimal), never float.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Column types, so precision is decided in one place.
Qty = Numeric(14, 3)    # 0.001 kg = 1 g, 0.001 L = 1 ml
Money = Numeric(14, 2)  # rupees with paise
Rate = Numeric(14, 4)   # unit costs such as weighted average 43.1250/kg


# ---------------------------------------------------------------- users

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    full_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))

    __table_args__ = (
        CheckConstraint(
            "role IN ('SALESMAN','ADMIN','PRODUCTION','AUDITOR','OWNER')",
            name="ck_user_role",
        ),
    )


# ---------------------------------------------------------------- units

class Unit(Base):
    """Global units. Units of the same dimension convert by `to_base`.

    mass: g=1, kg=1000 | volume: ml=1, l=1000 | count: pc=1, pkt=1
    """

    __tablename__ = "units"

    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    name: Mapped[str] = mapped_column(String(30))
    dimension: Mapped[str] = mapped_column(String(10))  # mass | volume | count
    to_base: Mapped[Decimal] = mapped_column(Numeric(14, 6))


class ItemUnitConversion(Base):
    """Item-specific units, e.g. 1 carton of Murukku 200 g = 20 packets.

    `factor` is how many of the item's stock_unit one of `unit_code` holds.
    """

    __tablename__ = "item_unit_conversions"

    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), primary_key=True)
    unit_code: Mapped[str] = mapped_column(String(10), primary_key=True)
    factor: Mapped[Decimal] = mapped_column(Numeric(14, 6))


# ---------------------------------------------------------------- items

class Item(Base):
    """Anything we hold stock of: raw material, packaging, or finished product."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))  # RAW | PACKAGING | FINISHED
    stock_unit: Mapped[str] = mapped_column(ForeignKey("units.code"))
    reorder_level: Mapped[Decimal | None] = mapped_column(Qty)

    # Finished goods only
    hsn_code: Mapped[str | None] = mapped_column(String(10))
    gst_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # percent
    sell_price: Mapped[Decimal | None] = mapped_column(Money)        # before tax
    recipe_id: Mapped[int | None] = mapped_column(ForeignKey("recipes.id"))
    net_weight_g: Mapped[Decimal | None] = mapped_column(Qty)        # pack size

    recipe: Mapped["Recipe | None"] = relationship()
    pack_lines: Mapped[list["PackLine"]] = relationship(
        foreign_keys="PackLine.product_id", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("kind IN ('RAW','PACKAGING','FINISHED')", name="ck_item_kind"),
    )


# ---------------------------------------------------------------- BOM
# The BOM is split in two so pack sizes can share a recipe:
#   Recipe   = ingredients for one batch -> output_g of product
#   PackLine = packaging per single packet of one pack size
# Expected packets per batch = recipe.output_g / product.net_weight_g
# (Murukku: 40000 g / 200 g = 200 packets; a 500 g pack would give 80.)

class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    output_g: Mapped[Decimal] = mapped_column(Qty)  # finished product per batch

    lines: Mapped[list["RecipeLine"]] = relationship(cascade="all, delete-orphan")


class RecipeLine(Base):
    __tablename__ = "recipe_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipe_id: Mapped[int] = mapped_column(ForeignKey("recipes.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)          # per batch
    unit: Mapped[str] = mapped_column(ForeignKey("units.code"))

    item: Mapped[Item] = relationship()


class PackLine(Base):
    __tablename__ = "pack_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty_per_packet: Mapped[Decimal] = mapped_column(Numeric(14, 6))  # carton = 0.05

    item: Mapped[Item] = relationship(foreign_keys=[item_id])


# ---------------------------------------------------------------- parties

class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(12))   # RETAILER | DISTRIBUTOR
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str] = mapped_column(String(2))  # GST state code, TN = 33
    address: Mapped[str | None] = mapped_column(String(300))
    salesman_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    gstin: Mapped[str | None] = mapped_column(String(15))
    state_code: Mapped[str] = mapped_column(String(2))


# ---------------------------------------------------------------- stock ledger

class StockMove(Base):
    """One row per stock movement. Balance of an item = SUM(qty).

    qty is signed and always in the item's stock_unit (+ in, - out).
    value = qty * unit_cost, so stock value = SUM(value) as well.
    """

    __tablename__ = "stock_moves"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(Qty)
    unit_cost: Mapped[Decimal] = mapped_column(Rate)
    value: Mapped[Decimal] = mapped_column(Money)
    move_type: Mapped[str] = mapped_column(String(20))
    source_type: Mapped[str] = mapped_column(String(20))
    source_id: Mapped[int | None]
    move_date: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    item: Mapped[Item] = relationship()

    __table_args__ = (
        CheckConstraint(
            "move_type IN ('OPENING','PURCHASE','PROD_ISSUE','PROD_OUTPUT',"
            "'DISPATCH','SALES_RETURN','ADJUSTMENT')",
            name="ck_move_type",
        ),
        CheckConstraint(
            "source_type IN ('OPENING','PURCHASE','PRODUCTION_RUN','INVOICE',"
            "'CREDIT_NOTE','ADJUSTMENT')",
            name="ck_source_type",
        ),
    )


# ---------------------------------------------------------------- purchases

class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    bill_no: Mapped[str] = mapped_column(String(30))
    bill_date: Mapped[date] = mapped_column(Date)
    taxable: Mapped[Decimal] = mapped_column(Money)
    tax: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    lines: Mapped[list["PurchaseLine"]] = relationship(cascade="all, delete-orphan")
    supplier: Mapped[Supplier] = relationship()


class PurchaseLine(Base):
    __tablename__ = "purchase_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("purchases.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)          # as entered
    unit: Mapped[str] = mapped_column(ForeignKey("units.code"))
    rate: Mapped[Decimal] = mapped_column(Rate)        # per entered unit
    amount: Mapped[Decimal] = mapped_column(Money)

    item: Mapped[Item] = relationship()


# ---------------------------------------------------------------- production

class ProductionRun(Base):
    __tablename__ = "production_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    run_date: Mapped[date] = mapped_column(Date)
    batches: Mapped[Decimal] = mapped_column(Qty)
    expected_output: Mapped[Decimal] = mapped_column(Qty)  # packets, from BOM
    actual_output: Mapped[Decimal] = mapped_column(Qty)    # good packets
    material_cost: Mapped[Decimal] = mapped_column(Money)
    batch_no: Mapped[str] = mapped_column(String(30), unique=True)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    product: Mapped[Item] = relationship()
    lines: Mapped[list["ProductionRunLine"]] = relationship(cascade="all, delete-orphan")


class ProductionRunLine(Base):
    """Planned vs actual consumption of one input in a run (stock units)."""

    __tablename__ = "production_run_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("production_runs.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    planned_qty: Mapped[Decimal] = mapped_column(Qty)
    actual_qty: Mapped[Decimal] = mapped_column(Qty)   # total issued incl. wasted
    wasted_qty: Mapped[Decimal] = mapped_column(Qty, default=Decimal("0"))
    unit_cost: Mapped[Decimal] = mapped_column(Rate)
    cost: Mapped[Decimal] = mapped_column(Money)

    item: Mapped[Item] = relationship()


class Overhead(Base):
    """Labour, gas/power etc. Either tied to a run, or a monthly pool."""

    __tablename__ = "overheads"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("production_runs.id"))
    month: Mapped[date] = mapped_column(Date)  # first day of the month
    category: Mapped[str] = mapped_column(String(20))  # LABOUR | POWER_GAS | OTHER
    amount: Mapped[Decimal] = mapped_column(Money)
    note: Mapped[str | None] = mapped_column(String(200))


# ---------------------------------------------------------------- sales

class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Generated on the phone; lets an offline order be re-sent without duplicating.
    client_ref: Mapped[str] = mapped_column(String(40), unique=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    salesman_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    order_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default="SUBMITTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reject_reason: Mapped[str | None] = mapped_column(String(200))

    customer: Mapped[Customer] = relationship()
    lines: Mapped[list["OrderLine"]] = relationship(cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('SUBMITTED','APPROVED','REJECTED','INVOICED','DISPATCHED','CANCELLED')",
            name="ck_order_status",
        ),
    )


class OrderLine(Base):
    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    qty: Mapped[Decimal] = mapped_column(Qty)   # packets
    rate: Mapped[Decimal] = mapped_column(Money)

    item: Mapped[Item] = relationship()


class DocCounter(Base):
    """Last number used per document series and financial year.

    ('INV', '2026-27') -> 1, 2, 3...   ('CN', '2026-27') -> 1, 2...
    Numbering restarts every 1 April: one series per financial year.
    """

    __tablename__ = "doc_counters"

    series: Mapped[str] = mapped_column(String(5), primary_key=True)
    fin_year: Mapped[str] = mapped_column(String(7), primary_key=True)
    last_no: Mapped[int] = mapped_column(default=0)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)  # INV/2026-27/0001
    fin_year: Mapped[str] = mapped_column(String(7))
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    invoice_date: Mapped[date] = mapped_column(Date)
    place_of_supply: Mapped[str] = mapped_column(String(2))  # state code
    taxable: Mapped[Decimal] = mapped_column(Money)
    cgst: Mapped[Decimal] = mapped_column(Money)
    sgst: Mapped[Decimal] = mapped_column(Money)
    igst: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[str] = mapped_column(String(10), default="POSTED")  # POSTED | CANCELLED
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    customer: Mapped[Customer] = relationship()
    lines: Mapped[list["InvoiceLine"]] = relationship(cascade="all, delete-orphan")


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id"))
    hsn_code: Mapped[str] = mapped_column(String(10))
    qty: Mapped[Decimal] = mapped_column(Qty)
    rate: Mapped[Decimal] = mapped_column(Money)
    taxable: Mapped[Decimal] = mapped_column(Money)
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    cgst: Mapped[Decimal] = mapped_column(Money)
    sgst: Mapped[Decimal] = mapped_column(Money)
    igst: Mapped[Decimal] = mapped_column(Money)

    item: Mapped[Item] = relationship()


class CreditNote(Base):
    """Posted invoices are never deleted; they are reversed by a credit note."""

    __tablename__ = "credit_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(30), unique=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"))
    note_date: Mapped[date] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(String(200))
    taxable: Mapped[Decimal] = mapped_column(Money)
    cgst: Mapped[Decimal] = mapped_column(Money)
    sgst: Mapped[Decimal] = mapped_column(Money)
    igst: Mapped[Decimal] = mapped_column(Money)
    total: Mapped[Decimal] = mapped_column(Money)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id"))
    amount: Mapped[Decimal] = mapped_column(Money)
    paid_on: Mapped[date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(10))  # CASH | UPI | BANK | CHEQUE
    reference: Mapped[str | None] = mapped_column(String(50))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (CheckConstraint("amount > 0", name="ck_payment_positive"),)


# ---------------------------------------------------------------- settings & audit

class Setting(Base):
    """Company details and tunables, e.g. variance_tolerance_pct = 3."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(String(200))


class AuditLog(Base):
    """Who changed what and when, with before/after snapshots."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    entity: Mapped[str] = mapped_column(String(30))
    entity_id: Mapped[int]
    action: Mapped[str] = mapped_column(String(20))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

