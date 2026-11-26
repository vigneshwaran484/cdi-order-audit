"""Production supervisor: purchases (raw material in), production runs, overheads."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Item, Overhead, ProductionRun, Purchase, Supplier, Unit, User
from app.services import costing
from app.services.production import IssueIn, expected_output, plan, record_run, run_summary
from app.services.purchases import PurchaseLineIn, record_purchase
from app.stock import balance
from app.web.admin import act
from app.web.common import back, day, dec, get_db, render, role

router = APIRouter()
floor = role("PRODUCTION", "ADMIN")


# ---------------------------------------------------------------- runs

@router.get("/production")
def runs(request: Request, user: User = Depends(floor), db: Session = Depends(get_db)):
    runs = db.scalars(select(ProductionRun).order_by(ProductionRun.id.desc()).limit(50)).all()
    summaries = [run_summary(db, r) for r in runs]
    products = db.scalars(select(Item).where(Item.kind == "FINISHED", Item.recipe_id.isnot(None))
                          .order_by(Item.name)).all()
    return render(request, "production.html", user, rows=list(zip(runs, summaries)),
                  products=products)


@router.get("/production/runs/new")
def new_run(request: Request, product_id: int, batches: str = "1",
            user: User = Depends(floor), db: Session = Depends(get_db)):
    product = db.get(Item, product_id)
    b = dec(batches, "Batches")
    exp = expected_output(product, b)
    lines = plan(db, product, b, exp)   # packaging pre-filled for the expected output
    units = {u.code: u for u in db.scalars(select(Unit))}
    on_hand = {ln.item_id: balance(db, ln.item_id) for ln in lines}
    return render(request, "production_new.html", user, product=product, batches=b,
                  expected=exp, lines=lines, on_hand=on_hand, units=units)


@router.post("/production/runs")
async def create_run(request: Request, user: User = Depends(floor),
                     db: Session = Depends(get_db)):
    form = await request.form()
    product_id = int(form["product_id"])
    url = f"/production/runs/new?product_id={product_id}&batches={form.get('batches', '1')}"

    def do():
        product = db.get(Item, product_id)
        issues = []
        for pl in plan(db, product, dec(form["batches"], "Batches"), Decimal("0")):
            iid = pl.item_id
            issues.append(IssueIn(iid, dec(form.get(f"issued_{iid}"), f"Issued {pl.item_name}"),
                                  form.get(f"unit_{iid}") or pl.unit,
                                  dec(form.get(f"wasted_{iid}"), "Wasted", Decimal("0"))))
        return record_run(db, product_id=product_id, run_date=day(form.get("run_date")),
                          batches=dec(form["batches"], "Batches"),
                          actual_output=dec(form.get("actual_output"), "Actual output"),
                          issues=issues, user_id=user.id)

    res = act(request, db, do, "Run {r.batch_no} saved: stock updated", url)
    return res if isinstance(res, Response) else back(f"/production/runs/{res.id}")


@router.get("/production/runs/{run_id}")
def run_detail(run_id: int, request: Request, user: User = Depends(floor),
               db: Session = Depends(get_db)):
    run = db.get(ProductionRun, run_id)
    cost = next(c for c in costing.run_costs(db, run.run_date, run.run_date) if c.run_id == run.id)
    ohs = db.scalars(select(Overhead).where(Overhead.run_id == run.id)).all()
    units = {ln.item_id: db.get(Item, ln.item_id).stock_unit for ln in run.lines}
    return render(request, "production_run.html", user, run=run, s=run_summary(db, run),
                  cost=cost, overheads=ohs, units=units)


@router.post("/production/overheads")
def add_overhead(request: Request, category: str = Form(...), amount: str = Form(...),
                 run_id: str = Form(""), month: str = Form(""), note: str = Form(""),
                 user: User = Depends(floor), db: Session = Depends(get_db)):
    url = f"/production/runs/{run_id}" if run_id else "/production"
    res = act(request, db, lambda: costing.add_overhead(
        db, category=category, amount=dec(amount, "Amount"), month=day(month or None),
        run_id=int(run_id) if run_id else None, note=note or None, user_id=user.id),
        "Overhead of Rs {r.amount} added", url)
    return res if isinstance(res, Response) else back(url)


# ---------------------------------------------------------------- purchases

@router.get("/production/purchases")
def purchases(request: Request, user: User = Depends(floor), db: Session = Depends(get_db)):
    rows = db.scalars(select(Purchase).order_by(Purchase.id.desc()).limit(50)).all()
    items = db.scalars(select(Item).where(Item.kind != "FINISHED").order_by(Item.kind, Item.name)).all()
    return render(request, "purchases.html", user, rows=rows, items=items,
                  suppliers=db.scalars(select(Supplier).order_by(Supplier.name)).all(),
                  units=db.scalars(select(Unit).order_by(Unit.dimension, Unit.code)).all())


@router.post("/production/purchases")
async def purchase_add(request: Request, user: User = Depends(floor),
                       db: Session = Depends(get_db)):
    form = await request.form()

    def do():
        lines = []
        for i in range(1, 4):
            if form.get(f"item_{i}") and form.get(f"qty_{i}"):
                lines.append(PurchaseLineIn(int(form[f"item_{i}"]), dec(form[f"qty_{i}"], "Qty"),
                                            form.get(f"unit_{i}") or "kg",
                                            dec(form.get(f"rate_{i}"), "Rate")))
        return record_purchase(db, supplier_id=int(form["supplier_id"]),
                               bill_no=form.get("bill_no", "").strip() or "NA",
                               bill_date=day(form.get("bill_date")), lines=lines,
                               user_id=user.id)

    res = act(request, db, do, "Purchase {r.bill_no} saved: stock increased",
              "/production/purchases")
    return res if isinstance(res, Response) else back("/production/purchases")
