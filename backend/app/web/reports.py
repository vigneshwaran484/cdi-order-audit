"""Reports (auditor), stock pages, owner dashboard and the audit log."""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import exports
from app.models import AuditLog, Customer, Item, Order, ProductionRun, User
from app.services import costing, reports
from app.services.production import run_summary
from app.services.sales import customer_outstanding, stock_check
from app.web.common import get_db, render, role, settings

router = APIRouter()
readers = role("ADMIN", "AUDITOR", "OWNER")
stock_viewers = role("PRODUCTION", "ADMIN", "AUDITOR", "OWNER")

LABELS = {
    "sales-register": "Sales register",
    "sales-by-customer": "Customer-wise sales",
    "sales-by-product": "Product-wise sales",
    "gst-summary": "GST summary (GSTR-1 / 3B)",
    "gst-hsn": "HSN summary",
    "purchase-register": "Purchase register",
    "stock-summary": "Stock summary",
    "stock-ledger": "Stock ledger",
    "production": "Production: planned vs actual",
    "cost-per-packet": "Cost per packet",
    "collections": "Payments received",
    "outstanding": "Outstanding with ageing",
}


def _period(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    t = date.fromisoformat(date_to) if date_to else date.today()
    f = date.fromisoformat(date_from) if date_from else costing.month_start(t)
    if f > t:
        raise HTTPException(400, "'From' is after 'To'")
    return f, t


def _download(r: reports.Report, fmt: str, company: str) -> Response:
    name = f"{r.key}_{r.date_from:%Y%m%d}-{r.date_to:%Y%m%d}"
    if fmt == "csv":
        return Response(exports.to_csv(r), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    if fmt == "xlsx":
        return Response(exports.to_xlsx(r), headers={
            "Content-Disposition": f'attachment; filename="{name}.xlsx"'},
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    if fmt == "pdf":
        return Response(exports.to_pdf(r, company), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{name}.pdf"'})
    raise HTTPException(400, "Format must be csv, xlsx or pdf")


@router.get("/reports")
def report_index(request: Request, key: str = "sales-register", date_from: str | None = None,
                 date_to: str | None = None, item_id: int | None = None, fmt: str | None = None,
                 user: User = Depends(readers), db: Session = Depends(get_db)):
    if key not in reports.REPORTS:
        raise HTTPException(404, "Unknown report")
    f, t = _period(date_from, date_to)
    r = (reports.stock_ledger(db, f, t, item_id) if key == "stock-ledger"
         else reports.REPORTS[key](db, f, t))
    if fmt:
        return _download(r, fmt, settings(db)["company_name"])
    items = db.scalars(select(Item).order_by(Item.kind, Item.code)).all()
    return render(request, "reports.html", user, r=r, key=key, labels=LABELS, f=f, t=t,
                  items=items, item_id=item_id, rows=r.with_totals())


@router.get("/stock")
def stock(request: Request, user: User = Depends(stock_viewers), db: Session = Depends(get_db)):
    t = date.today()
    r = reports.stock_summary(db, costing.month_start(t), t)
    return render(request, "stock.html", user, r=r, t=t,
                  ids={i.code: i.id for i in db.scalars(select(Item))})


@router.get("/stock/{item_id}")
def stock_item(item_id: int, request: Request, user: User = Depends(stock_viewers),
               db: Session = Depends(get_db)):
    r = reports.stock_ledger(db, date(2000, 1, 1), date.today(), item_id)
    return render(request, "stock_item.html", user, r=r, item=db.get(Item, item_id))


@router.get("/dashboard")
def dashboard(request: Request, user: User = Depends(role("OWNER", "ADMIN")),
              db: Session = Depends(get_db)):
    today = date.today()
    m0 = costing.month_start(today)
    todays = db.scalars(select(Order).where(Order.order_date == today)).all()
    pending = db.scalars(select(Order).where(Order.status == "SUBMITTED")).all()
    short = [o for o in pending if not all(c.ok for c in stock_check(db, o))]
    stock = reports.stock_summary(db, m0, today).rows
    alerts = []
    for run in db.scalars(select(ProductionRun).where(ProductionRun.run_date >= m0)
                          .order_by(ProductionRun.id.desc())):
        s = run_summary(db, run)
        if s["yield_flagged"]:
            alerts.append((run, "Yield", s["yield_variance_pct"]))
        alerts += [(run, ln["item"], ln["variance_pct"]) for ln in s["lines"] if ln["flagged"]]
    outstanding = [(c, customer_outstanding(db, c.id)) for c in db.scalars(select(Customer))]
    month_sales = reports.sales_register(db, m0, today).rows
    return render(
        request, "dashboard.html", user, today=today, todays=todays, pending=pending,
        short=short, stock=stock, alerts=alerts, costs=costing.product_costs(db, m0, today),
        outstanding=[o for o in outstanding if o[1] != 0],
        total_out=sum((o[1] for o in outstanding), Decimal("0")),
        month_taxable=sum((r["taxable"] for r in month_sales), Decimal("0")),
    )


@router.get("/audit-log")
def audit_log(request: Request, user: User = Depends(readers), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)).all()
    names = dict(db.execute(select(User.id, User.full_name)).all())
    total = db.scalar(select(func.count(AuditLog.id)))
    return render(request, "audit_log.html", user, rows=rows, names=names, total=total)
