"""Purchases: raw/packaging material received from a supplier -> stock goes up."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Item, Purchase, PurchaseLine
from app.services import audit
from app.stock import money, post_move
from app.units import to_stock_unit


@dataclass
class PurchaseLineIn:
    item_id: int
    qty: Decimal
    unit: str
    rate: Decimal  # per `unit`, before tax


def record_purchase(
    db: Session,
    *,
    supplier_id: int,
    bill_no: str,
    bill_date: date,
    lines: list[PurchaseLineIn],
    tax: Decimal = Decimal("0"),
    user_id: int | None = None,
) -> Purchase:
    if not lines:
        raise ValueError("A purchase needs at least one line")

    purchase = Purchase(
        supplier_id=supplier_id, bill_no=bill_no, bill_date=bill_date,
        taxable=Decimal("0"), tax=money(tax), created_by=user_id,
    )
    db.add(purchase)
    db.flush()

    total = Decimal("0")
    for ln in lines:
        item = db.get(Item, ln.item_id)
        if item is None or item.kind == "FINISHED":
            raise ValueError("Purchases are for raw or packaging items only")
        qty, rate = Decimal(ln.qty), Decimal(ln.rate)
        if qty <= 0 or rate < 0:
            raise ValueError("Quantity must be positive and rate non-negative")

        amount = money(qty * rate)
        stock_qty = to_stock_unit(db, item, qty, ln.unit)
        purchase.lines.append(PurchaseLine(
            item_id=item.id, qty=qty, unit=ln.unit, rate=rate, amount=amount,
        ))
        # Ledger: +qty in stock units, valued at exactly the bill amount.
        post_move(
            db, item_id=item.id, qty=stock_qty, unit_cost=amount / stock_qty,
            value=amount, move_type="PURCHASE", source_type="PURCHASE",
            source_id=purchase.id, move_date=bill_date,
            note=f"Bill {bill_no}", user_id=user_id,
        )
        total += amount

    purchase.taxable = total
    audit.log(db, user_id=user_id, entity="purchase", entity_id=purchase.id,
              action="CREATE", after={"bill_no": bill_no, "taxable": total})
    return purchase
