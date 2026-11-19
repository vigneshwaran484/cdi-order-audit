"""Production runs: issue materials against the BOM, receive finished packets.

Planned quantities:
- Recipe inputs (flour, oil, spice): recipe qty x batches. The recipe is the
  standard for a batch, so over- or under-use shows up as variance.
- Packaging (pouches, cartons): based on the ACTUAL good output, because a
  pouch is only needed for a packet that was actually made. Count items are
  rounded down to whole pieces: 188 packets fill 9 full cartons (8 loose).

Expected output (packets) = recipe.output_g x batches / product.net_weight_g.

Saving a run (one DB transaction):
  every input  -> stock move PROD_ISSUE  (-actual_qty, at weighted avg cost)
  the product  -> stock move PROD_OUTPUT (+actual_output, at material cost)
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Item, ProductionRun, ProductionRunLine, Setting, Unit
from app.services import audit
from app.stock import RATE_PLACES, avg_cost, post_move
from app.units import to_stock_unit

QTY_PLACES = Decimal("0.001")


@dataclass
class PlannedLine:
    item_id: int
    item_name: str
    unit: str            # item's stock unit
    planned_qty: Decimal
    source: str          # RECIPE | PACKAGING


@dataclass
class IssueIn:
    """What the supervisor actually issued for one input (in `unit`)."""
    item_id: int
    qty: Decimal         # total issued, including damaged/wasted
    unit: str
    wasted_qty: Decimal = Decimal("0")


def expected_output(product: Item, batches: Decimal) -> Decimal:
    recipe = product.recipe
    if recipe is None or not product.net_weight_g:
        raise ValueError(f"{product.name} has no recipe or pack size")
    return (recipe.output_g * Decimal(batches) / product.net_weight_g).quantize(QTY_PLACES)


def plan(db: Session, product: Item, batches: Decimal, output: Decimal) -> list[PlannedLine]:
    """Standard consumption for `batches` of recipe and `output` good packets."""
    lines: list[PlannedLine] = []
    for rl in product.recipe.lines:
        qty = to_stock_unit(db, rl.item, rl.qty * Decimal(batches), rl.unit)
        lines.append(PlannedLine(rl.item_id, rl.item.name, rl.item.stock_unit,
                                 qty.quantize(QTY_PLACES), "RECIPE"))
    for pl in product.pack_lines:
        qty = pl.qty_per_packet * Decimal(output)
        if db.get(Unit, pl.item.stock_unit).dimension == "count":
            qty = qty.to_integral_value(rounding=ROUND_FLOOR)
        lines.append(PlannedLine(pl.item_id, pl.item.name, pl.item.stock_unit,
                                 qty.quantize(QTY_PLACES), "PACKAGING"))
    return lines


def variance_pct(planned: Decimal, actual: Decimal) -> Decimal | None:
    if planned == 0:
        return None
    return ((actual - planned) / planned * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def tolerance_pct(db: Session) -> Decimal:
    s = db.get(Setting, "variance_tolerance_pct")
    return Decimal(s.value) if s else Decimal("3")


def _next_batch_no(db: Session, product: Item, run_date: date) -> str:
    prefix = f"{product.code}-{run_date:%Y%m%d}"
    n = db.scalar(select(func.count()).where(ProductionRun.batch_no.like(f"{prefix}-%")))
    return f"{prefix}-{n + 1:02d}"


def record_run(
    db: Session,
    *,
    product_id: int,
    run_date: date,
    batches: Decimal,
    actual_output: Decimal,
    issues: list[IssueIn],
    user_id: int | None = None,
) -> ProductionRun:
    product = db.get(Item, product_id)
    if product is None or product.kind != "FINISHED":
        raise ValueError("Pick a finished product")
    batches, actual_output = Decimal(batches), Decimal(actual_output)
    if batches <= 0 or actual_output < 0:
        raise ValueError("Batches must be positive and output non-negative")

    planned = {p.item_id: p for p in plan(db, product, batches, actual_output)}
    issued = {i.item_id: i for i in issues}
    unknown = set(issued) - set(planned)
    if unknown:
        raise ValueError(f"Items {sorted(unknown)} are not in this product's BOM")

    run = ProductionRun(
        product_id=product.id, run_date=run_date, batches=batches,
        expected_output=expected_output(product, batches),
        actual_output=actual_output, material_cost=Decimal("0"),
        batch_no=_next_batch_no(db, product, run_date), created_by=user_id,
    )
    db.add(run)
    db.flush()

    material_cost = Decimal("0")
    for item_id, p in planned.items():
        item = db.get(Item, item_id)
        iss = issued.get(item_id)
        # An input left out of the form is assumed issued exactly as planned.
        actual = to_stock_unit(db, item, iss.qty, iss.unit) if iss else p.planned_qty
        wasted = to_stock_unit(db, item, iss.wasted_qty, iss.unit) if iss else Decimal("0")
        if actual < 0 or wasted < 0 or wasted > actual:
            raise ValueError(f"Bad quantities for {item.name}")

        cost = Decimal("0")
        unit_cost = avg_cost(db, item_id)
        if actual > 0:
            move = post_move(
                db, item_id=item_id, qty=-actual, move_type="PROD_ISSUE",
                source_type="PRODUCTION_RUN", source_id=run.id, move_date=run_date,
                note=f"Issued to {run.batch_no}", user_id=user_id,
            )
            cost = -move.value
            unit_cost = move.unit_cost
        run.lines.append(ProductionRunLine(
            item_id=item_id, planned_qty=p.planned_qty, actual_qty=actual,
            wasted_qty=wasted, unit_cost=unit_cost, cost=cost,
        ))
        material_cost += cost

    run.material_cost = material_cost
    if actual_output > 0:
        post_move(
            db, item_id=product.id, qty=actual_output,
            unit_cost=(material_cost / actual_output).quantize(RATE_PLACES, rounding=ROUND_HALF_UP),
            value=material_cost, move_type="PROD_OUTPUT",
            source_type="PRODUCTION_RUN", source_id=run.id, move_date=run_date,
            note=f"Output of {run.batch_no}", user_id=user_id,
        )

    audit.log(db, user_id=user_id, entity="production_run", entity_id=run.id,
              action="CREATE", after=run_summary(db, run))
    return run


def run_summary(db: Session, run: ProductionRun) -> dict:
    """Planned vs actual for every input plus yield, with variance flags."""
    tol = tolerance_pct(db)
    lines = []
    for ln in run.lines:
        v = variance_pct(ln.planned_qty, ln.actual_qty)
        lines.append({
            "item_id": ln.item_id,
            "item": db.get(Item, ln.item_id).name,
            "planned": ln.planned_qty,
            "actual": ln.actual_qty,
            "wasted": ln.wasted_qty,
            "variance_pct": v,
            "flagged": v is not None and abs(v) > tol,
            "cost": ln.cost,
        })
    yv = variance_pct(run.expected_output, run.actual_output)
    return {
        "run_id": run.id,
        "batch_no": run.batch_no,
        "expected_output": run.expected_output,
        "actual_output": run.actual_output,
        "yield_variance_pct": yv,
        "yield_flagged": yv is not None and abs(yv) > tol,
        "tolerance_pct": tol,
        "material_cost": run.material_cost,
        "lines": lines,
    }
