"""The stock ledger: the only place stock changes.

Valuation method: perpetual WEIGHTED AVERAGE cost.
Every receipt (purchase, production output) adds qty and value; every issue
leaves at the current average = total value / total qty of that item.
Why weighted average: a snack factory mixes flour from different purchases in
one bin, so there is no physical "first lot" to trace; average cost matches
how the material is actually used and is accepted under AS-2 / Ind AS 2.
"""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import StockMove

PAISA = Decimal("0.01")
RATE_PLACES = Decimal("0.0001")


def money(x: Decimal) -> Decimal:
    return Decimal(x).quantize(PAISA, rounding=ROUND_HALF_UP)


class InsufficientStock(ValueError):
    def __init__(self, item_name: str, needed: Decimal, available: Decimal):
        self.item_name = item_name
        self.needed = needed
        self.available = available
        self.shortage = needed - available
        super().__init__(
            f"Insufficient stock of {item_name}: need {needed}, "
            f"have {available}, short by {self.shortage}"
        )


def balance(db: Session, item_id: int, as_of: date | None = None) -> Decimal:
    q = select(func.coalesce(func.sum(StockMove.qty), 0)).where(StockMove.item_id == item_id)
    if as_of is not None:
        q = q.where(StockMove.move_date <= as_of)
    return Decimal(db.scalar(q))


def stock_value(db: Session, item_id: int) -> Decimal:
    q = select(func.coalesce(func.sum(StockMove.value), 0)).where(StockMove.item_id == item_id)
    return Decimal(db.scalar(q))


def avg_cost(db: Session, item_id: int) -> Decimal:
    """Current weighted-average cost per stock unit."""
    qty = balance(db, item_id)
    if qty > 0:
        return (stock_value(db, item_id) / qty).quantize(RATE_PLACES, rounding=ROUND_HALF_UP)
    # Nothing left: fall back to the last receipt's cost.
    last = db.scalar(
        select(StockMove.unit_cost)
        .where(StockMove.item_id == item_id, StockMove.qty > 0)
        .order_by(StockMove.id.desc())
        .limit(1)
    )
    return Decimal(last or 0)


def post_move(
    db: Session,
    *,
    item_id: int,
    qty: Decimal,
    move_type: str,
    source_type: str,
    source_id: int | None,
    move_date: date,
    unit_cost: Decimal | None = None,
    note: str | None = None,
    user_id: int | None = None,
    allow_negative: bool = False,
) -> StockMove:
    """Write one ledger row.

    Receipts (qty > 0) must pass unit_cost. Issues (qty < 0) are costed at the
    current weighted average and are refused if they would go below zero.
    """
    qty = Decimal(qty)
    if qty < 0:
        available = balance(db, item_id)
        if not allow_negative and available + qty < 0:
            from app.models import Item

            raise InsufficientStock(db.get(Item, item_id).name, -qty, available)
        if unit_cost is None:
            unit_cost = avg_cost(db, item_id)
        # If this issue empties the item, take the exact remaining value so
        # no stray paise are left behind by rounding.
        if available + qty == 0:
            value = -stock_value(db, item_id)
        else:
            value = money(qty * unit_cost)
    else:
        if unit_cost is None:
            raise ValueError("Receipts need a unit_cost")
        value = money(qty * unit_cost)

    move = StockMove(
        item_id=item_id,
        qty=qty,
        unit_cost=Decimal(unit_cost),
        value=value,
        move_type=move_type,
        source_type=source_type,
        source_id=source_id,
        move_date=move_date,
        note=note,
        created_by=user_id,
    )
    db.add(move)
    db.flush()  # so the next balance() in this transaction sees it
    return move
