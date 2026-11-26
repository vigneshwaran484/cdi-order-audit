"""Overheads and cost per packet.

Material cost of a run is already known exactly: every input left stock at
its weighted-average cost (see app/stock.py), and the run stores the total.

Overheads (labour, gas/power, other) are entered in one of two ways:
- DIRECT: tied to a production run ("labour Rs 1,200 for this run").
  It goes 100% to that run.
- MONTHLY POOL: not tied to a run (e.g. the month's electricity bill).
  It is shared between that month's runs in proportion to the KILOGRAMS of
  good product each run made (good packets x pack weight).
  Why kg: labour, frying gas and power scale with how much dough is fried,
  not with how many pouches it is packed into, so a 500 g pack should carry
  2.5x the overhead of a 200 g pack.

Cost per packet of a run = (material + direct overheads + pool share) / good packets.
Wasted material is already inside the material cost, so wastage raises the
cost of the good packets, which is what really happens.

Finished-goods STOCK is valued at material cost only (overheads are added
in this report, not in the ledger), because overheads are often entered
days after the run; changing the value of packets already sold would make
the ledger drift. This is stated in the README.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Overhead, ProductionRun
from app.services import audit
from app.stock import money

CATEGORIES = ("LABOUR", "POWER_GAS", "OTHER")
COST_PLACES = Decimal("0.01")


def month_start(d: date) -> date:
    return d.replace(day=1)


def next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def add_overhead(
    db: Session,
    *,
    category: str,
    amount: Decimal,
    month: date,
    run_id: int | None = None,
    note: str | None = None,
    user_id: int | None = None,
) -> Overhead:
    if category not in CATEGORIES:
        raise ValueError(f"Category must be one of {', '.join(CATEGORIES)}")
    amount = money(Decimal(amount))
    if amount <= 0:
        raise ValueError("Overhead amount must be more than zero")
    if run_id is not None:
        run = db.get(ProductionRun, run_id)
        if run is None:
            raise ValueError("Unknown production run")
        month = run.run_date
    oh = Overhead(run_id=run_id, month=month_start(month), category=category,
                  amount=amount, note=note)
    db.add(oh)
    db.flush()
    audit.log(db, user_id=user_id, entity="overhead", entity_id=oh.id, action="CREATE",
              after={"category": category, "amount": amount, "run_id": run_id})
    return oh


@dataclass
class RunCost:
    run_id: int
    batch_no: str
    run_date: date
    product_id: int
    product: str
    good_packets: Decimal
    output_kg: Decimal
    material: Decimal
    direct_overhead: Decimal
    pool_overhead: Decimal

    @property
    def total(self) -> Decimal:
        return self.material + self.direct_overhead + self.pool_overhead

    @property
    def per_packet(self) -> Decimal | None:
        if self.good_packets == 0:
            return None
        return (self.total / self.good_packets).quantize(COST_PLACES, rounding=ROUND_HALF_UP)

    @property
    def material_per_packet(self) -> Decimal | None:
        if self.good_packets == 0:
            return None
        return (self.material / self.good_packets).quantize(COST_PLACES, rounding=ROUND_HALF_UP)


def run_costs(db: Session, date_from: date, date_to: date) -> list[RunCost]:
    """Cost of every run in [date_from, date_to], with overheads allocated."""
    runs = db.scalars(
        select(ProductionRun)
        .where(ProductionRun.run_date >= date_from, ProductionRun.run_date <= date_to)
        .order_by(ProductionRun.run_date, ProductionRun.id)
    ).all()

    costs: list[RunCost] = []
    for r in runs:
        product = db.get(Item, r.product_id)
        direct = sum(
            (o.amount for o in db.scalars(select(Overhead).where(Overhead.run_id == r.id))),
            Decimal("0"),
        )
        kg = (r.actual_output * (product.net_weight_g or 0) / 1000).quantize(Decimal("0.001"))
        costs.append(RunCost(r.id, r.batch_no, r.run_date, product.id, product.name,
                             r.actual_output, kg, r.material_cost, money(direct), Decimal("0.00")))

    # Share each month's pool across ALL runs of that month (not only the
    # ones inside the date filter), so the share doesn't depend on the filter.
    months = sorted({month_start(c.run_date) for c in costs})
    for m in months:
        pool = sum((o.amount for o in db.scalars(select(Overhead).where(
            Overhead.run_id.is_(None), Overhead.month == m))), Decimal("0"))
        if pool == 0:
            continue
        month_runs = db.scalars(select(ProductionRun).where(
            ProductionRun.run_date >= m, ProductionRun.run_date < next_month(m))).all()
        weights = {
            r.id: r.actual_output * (db.get(Item, r.product_id).net_weight_g or 0)
            for r in month_runs
        }
        total_w = sum(weights.values(), Decimal("0"))
        if total_w == 0:
            continue
        for c in costs:
            if month_start(c.run_date) == m:
                c.pool_overhead = money(pool * weights[c.run_id] / total_w)
    return costs


@dataclass
class ProductCost:
    product_id: int
    product: str
    good_packets: Decimal
    material: Decimal
    overhead: Decimal
    sell_price: Decimal

    @property
    def per_packet(self) -> Decimal | None:
        if self.good_packets == 0:
            return None
        return ((self.material + self.overhead) / self.good_packets).quantize(
            COST_PLACES, rounding=ROUND_HALF_UP)

    @property
    def margin(self) -> Decimal | None:
        return None if self.per_packet is None else self.sell_price - self.per_packet

    @property
    def margin_pct(self) -> Decimal | None:
        if self.margin is None or not self.sell_price:
            return None
        return (self.margin / self.sell_price * 100).quantize(COST_PLACES, rounding=ROUND_HALF_UP)


def product_costs(db: Session, date_from: date, date_to: date) -> list[ProductCost]:
    """Cost per packet by product = all its runs' costs / all its good packets."""
    by_product: dict[int, ProductCost] = {}
    for c in run_costs(db, date_from, date_to):
        pc = by_product.get(c.product_id)
        if pc is None:
            item = db.get(Item, c.product_id)
            pc = by_product[c.product_id] = ProductCost(
                c.product_id, c.product, Decimal("0"), Decimal("0"), Decimal("0"),
                item.sell_price or Decimal("0"))
        pc.good_packets += c.good_packets
        pc.material += c.material
        pc.overhead += c.direct_overhead + c.pool_overhead
    return list(by_product.values())
