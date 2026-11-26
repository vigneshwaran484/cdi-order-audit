"""Monthly reports for accounts and audit (problem statement section 4.6).

Every report returns the same shape (Report: columns + rows), so one exporter
turns any of them into CSV, Excel or PDF (app/exports.py).
All money stays Decimal end to end; nothing is converted to float.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CreditNote,
    Customer,
    Invoice,
    InvoiceLine,
    Item,
    Payment,
    ProductionRun,
    Purchase,
    StockMove,
)
from app.services import costing
from app.services.production import run_summary

ZERO = Decimal("0")


@dataclass
class Report:
    key: str
    title: str
    columns: list[tuple[str, str]]          # (key, label)
    rows: list[dict]
    date_from: date
    date_to: date
    notes: list[str] = field(default_factory=list)
    total_keys: tuple[str, ...] = ()       # columns to add up in a TOTAL row

    def with_totals(self) -> list[dict]:
        if not self.total_keys or not self.rows:
            return self.rows
        total = {k: sum((r.get(k) or ZERO for r in self.rows), ZERO) for k in self.total_keys}
        first = self.columns[0][0]
        total[first] = "TOTAL"
        return [*self.rows, total]


# ---------------------------------------------------------------- sales

def _invoices(db: Session, f: date, t: date) -> list[Invoice]:
    return db.scalars(select(Invoice).where(Invoice.invoice_date >= f, Invoice.invoice_date <= t)
                      .order_by(Invoice.invoice_date, Invoice.number)).all()


def _credit_notes(db: Session, f: date, t: date) -> list[CreditNote]:
    return db.scalars(select(CreditNote).where(CreditNote.note_date >= f, CreditNote.note_date <= t)
                      .order_by(CreditNote.note_date, CreditNote.number)).all()


def sales_register(db: Session, f: date, t: date) -> Report:
    """Every invoice in the period; credit notes appear as negative rows,
    so a cancelled sale nets to zero without anything being deleted."""
    rows = []
    for inv in _invoices(db, f, t):
        rows.append({
            "date": inv.invoice_date, "doc": inv.number, "customer": inv.customer.name,
            "gstin": inv.customer.gstin or "", "pos": inv.place_of_supply,
            "taxable": inv.taxable, "cgst": inv.cgst, "sgst": inv.sgst, "igst": inv.igst,
            "total": inv.total, "status": inv.status,
        })
    for cn in _credit_notes(db, f, t):
        inv = db.get(Invoice, cn.invoice_id)
        rows.append({
            "date": cn.note_date, "doc": f"{cn.number} (against {inv.number})",
            "customer": inv.customer.name, "gstin": inv.customer.gstin or "",
            "pos": inv.place_of_supply, "taxable": -cn.taxable, "cgst": -cn.cgst,
            "sgst": -cn.sgst, "igst": -cn.igst, "total": -cn.total, "status": "CREDIT NOTE",
        })
    rows.sort(key=lambda r: (r["date"], r["doc"]))
    return Report(
        "sales-register", "Sales register",
        [("date", "Date"), ("doc", "Invoice / CN"), ("customer", "Customer"),
         ("gstin", "GSTIN"), ("pos", "POS"), ("taxable", "Taxable"), ("cgst", "CGST"),
         ("sgst", "SGST"), ("igst", "IGST"), ("total", "Total"), ("status", "Status")],
        rows, f, t, total_keys=("taxable", "cgst", "sgst", "igst", "total"),
    )


def _net_lines(db: Session, f: date, t: date):
    """(invoice, line, sign): invoice lines in the period, plus the lines of
    invoices cancelled by a credit note in the period with sign -1."""
    for inv in _invoices(db, f, t):
        for ln in inv.lines:
            yield inv, ln, 1
    for cn in _credit_notes(db, f, t):
        inv = db.get(Invoice, cn.invoice_id)
        for ln in inv.lines:
            yield inv, ln, -1


def sales_by_customer(db: Session, f: date, t: date) -> Report:
    agg: dict[int, dict] = {}
    for inv, ln, sign in _net_lines(db, f, t):
        r = agg.setdefault(inv.customer_id, {
            "customer": inv.customer.name, "kind": inv.customer.kind,
            "invoices": set(), "qty": ZERO, "taxable": ZERO, "tax": ZERO, "total": ZERO})
        r["invoices"].add(inv.number)
        tax = ln.cgst + ln.sgst + ln.igst
        r["qty"] += sign * ln.qty
        r["taxable"] += sign * ln.taxable
        r["tax"] += sign * tax
        r["total"] += sign * (ln.taxable + tax)
    rows = [{**r, "invoices": len(r["invoices"])} for r in agg.values()]
    return Report(
        "sales-by-customer", "Customer-wise sales",
        [("customer", "Customer"), ("kind", "Type"), ("invoices", "Invoices"),
         ("qty", "Packets"), ("taxable", "Taxable"), ("tax", "GST"), ("total", "Total")],
        rows, f, t, total_keys=("qty", "taxable", "tax", "total"),
        notes=["Net of credit notes."],
    )


def sales_by_product(db: Session, f: date, t: date) -> Report:
    agg: dict[int, dict] = {}
    for _, ln, sign in _net_lines(db, f, t):
        r = agg.setdefault(ln.item_id, {
            "product": ln.item.name, "hsn": ln.hsn_code, "qty": ZERO, "taxable": ZERO})
        r["qty"] += sign * ln.qty
        r["taxable"] += sign * ln.taxable
    rows = list(agg.values())
    for r in rows:
        r["avg_rate"] = (r["taxable"] / r["qty"]).quantize(Decimal("0.01")) if r["qty"] else ZERO
    return Report(
        "sales-by-product", "Product-wise sales",
        [("product", "Product"), ("hsn", "HSN"), ("qty", "Packets"),
         ("avg_rate", "Avg rate"), ("taxable", "Taxable")],
        rows, f, t, total_keys=("qty", "taxable"), notes=["Net of credit notes."],
    )


# ---------------------------------------------------------------- GST

def gst_summary(db: Session, f: date, t: date) -> Report:
    """Outward supplies in the shape of GSTR-3B table 3.1(a) and the
    GSTR-1 sections (B2B, B2C, credit notes), split intra/inter-state."""
    buckets: dict[str, dict] = {}

    def add(section: str, desc: str, taxable, cgst, sgst, igst, sign=1):
        b = buckets.setdefault(section, {"section": section, "description": desc,
                                         "taxable": ZERO, "cgst": ZERO, "sgst": ZERO,
                                         "igst": ZERO, "count": 0})
        b["taxable"] += sign * taxable
        b["cgst"] += sign * cgst
        b["sgst"] += sign * sgst
        b["igst"] += sign * igst
        b["count"] += 1

    for inv in _invoices(db, f, t):
        b2b = bool(inv.customer.gstin)
        sec = "GSTR-1 B2B (4A)" if b2b else (
            "GSTR-1 B2C small (7)" if inv.cgst else "GSTR-1 B2C inter-state (5/7)")
        add(sec, "Invoices to registered buyers" if b2b else "Invoices to unregistered buyers",
            inv.taxable, inv.cgst, inv.sgst, inv.igst)
    for cn in _credit_notes(db, f, t):
        inv = db.get(Invoice, cn.invoice_id)
        sec = "GSTR-1 CDNR (9B)" if inv.customer.gstin else "GSTR-1 CDNUR (9B)"
        add(sec, "Credit notes (reduce tax)", cn.taxable, cn.cgst, cn.sgst, cn.igst, sign=-1)

    rows = sorted(buckets.values(), key=lambda r: r["section"])
    net = {k: sum((r[k] for r in rows), ZERO) for k in ("taxable", "cgst", "sgst", "igst")}
    rows.append({"section": "GSTR-3B 3.1(a)", "description":
                 "Outward taxable supplies, net of credit notes", **net, "count": ""})
    return Report(
        "gst-summary", "GST summary of outward supplies",
        [("section", "Return / table"), ("description", "What"), ("count", "Docs"),
         ("taxable", "Taxable"), ("igst", "IGST"), ("cgst", "CGST"), ("sgst", "SGST")],
        rows, f, t,
        notes=["Place of supply = buyer's state. Same state as the factory (33, Tamil Nadu) "
               "-> CGST + SGST; other states -> IGST.",
               "The last row is the figure for GSTR-3B table 3.1(a)."],
    )


def gst_hsn_summary(db: Session, f: date, t: date) -> Report:
    """GSTR-1 table 12: HSN-wise summary of outward supplies."""
    agg: dict[tuple, dict] = {}
    for _, ln, sign in _net_lines(db, f, t):
        r = agg.setdefault((ln.hsn_code, ln.gst_rate), {
            "hsn": ln.hsn_code, "desc": ln.item.name, "uqc": "PAC", "rate": ln.gst_rate,
            "qty": ZERO, "taxable": ZERO, "igst": ZERO, "cgst": ZERO, "sgst": ZERO})
        for k in ("qty", "taxable", "igst", "cgst", "sgst"):
            r[k] += sign * getattr(ln, k)
    return Report(
        "gst-hsn", "HSN summary (GSTR-1 table 12)",
        [("hsn", "HSN"), ("desc", "Description"), ("uqc", "UQC"), ("rate", "Rate %"),
         ("qty", "Qty"), ("taxable", "Taxable"), ("igst", "IGST"), ("cgst", "CGST"),
         ("sgst", "SGST")],
        list(agg.values()), f, t, total_keys=("taxable", "igst", "cgst", "sgst"),
    )


# ---------------------------------------------------------------- purchases

def purchase_register(db: Session, f: date, t: date) -> Report:
    rows = []
    purchases = db.scalars(select(Purchase).where(Purchase.bill_date >= f, Purchase.bill_date <= t)
                           .order_by(Purchase.bill_date, Purchase.id)).all()
    for p in purchases:
        for ln in p.lines:
            rows.append({"date": p.bill_date, "bill": p.bill_no, "supplier": p.supplier.name,
                         "item": ln.item.name, "qty": ln.qty, "unit": ln.unit,
                         "rate": ln.rate, "amount": ln.amount})
    return Report(
        "purchase-register", "Purchase register",
        [("date", "Date"), ("bill", "Bill no"), ("supplier", "Supplier"), ("item", "Item"),
         ("qty", "Qty"), ("unit", "Unit"), ("rate", "Rate"), ("amount", "Amount")],
        rows, f, t, total_keys=("amount",), notes=["Amounts are before input tax."],
    )


# ---------------------------------------------------------------- stock

def source_label(db: Session, move: StockMove) -> str:
    """Human name of the document behind a stock move (traceability)."""
    st, sid = move.source_type, move.source_id
    if st == "OPENING":
        return "Opening balance"
    if st == "PURCHASE":
        p = db.get(Purchase, sid)
        return f"Purchase bill {p.bill_no}" if p else f"Purchase #{sid}"
    if st == "PRODUCTION_RUN":
        r = db.get(ProductionRun, sid)
        return f"Production run {r.batch_no}" if r else f"Run #{sid}"
    if st == "INVOICE":
        i = db.get(Invoice, sid)
        return f"Invoice {i.number}" if i else f"Invoice #{sid}"
    if st == "CREDIT_NOTE":
        c = db.get(CreditNote, sid)
        return f"Credit note {c.number}" if c else f"Credit note #{sid}"
    return f"{st} #{sid}"


def _sum(db: Session, col, item_id: int, *conds) -> Decimal:
    return Decimal(db.scalar(select(func.coalesce(func.sum(col), 0))
                             .where(StockMove.item_id == item_id, *conds)))


def stock_summary(db: Session, f: date, t: date, kind: str | None = None) -> Report:
    """opening + receipts - issues +/- adjustments = closing, per item."""
    q = select(Item).order_by(Item.kind, Item.code)
    if kind:
        q = q.where(Item.kind == kind)
    rows = []
    in_period = (StockMove.move_date >= f, StockMove.move_date <= t)
    for it in db.scalars(q):
        opening = _sum(db, StockMove.qty, it.id, StockMove.move_date < f) + _sum(
            db, StockMove.qty, it.id, StockMove.move_type == "OPENING", *in_period)
        receipts = _sum(db, StockMove.qty, it.id, StockMove.qty > 0,
                        StockMove.move_type.notin_(("OPENING", "ADJUSTMENT")), *in_period)
        issues = -_sum(db, StockMove.qty, it.id, StockMove.qty < 0,
                       StockMove.move_type != "ADJUSTMENT", *in_period)
        adjust = _sum(db, StockMove.qty, it.id, StockMove.move_type == "ADJUSTMENT", *in_period)
        closing = _sum(db, StockMove.qty, it.id, StockMove.move_date <= t)
        value = _sum(db, StockMove.value, it.id, StockMove.move_date <= t)
        assert opening + receipts - issues + adjust == closing, it.code
        rows.append({
            "code": it.code, "item": it.name, "kind": it.kind, "unit": it.stock_unit,
            "opening": opening, "receipts": receipts, "issues": issues, "adjust": adjust,
            "closing": closing, "value": value,
            "low": "LOW" if it.reorder_level is not None and closing < it.reorder_level else "",
        })
    return Report(
        "stock-summary", "Stock summary",
        [("code", "Code"), ("item", "Item"), ("kind", "Type"), ("unit", "Unit"),
         ("opening", "Opening"), ("receipts", "Receipts"), ("issues", "Issues/Sales"),
         ("adjust", "Adj."), ("closing", "Closing"), ("value", "Value (Rs)"),
         ("low", "Reorder")],
        rows, f, t, total_keys=("value",),
        notes=["Opening + Receipts - Issues/Sales + Adj. = Closing, all summed from the "
               "stock ledger.", "Valuation: weighted average cost. Finished goods at "
               "material cost (overheads are in the cost-per-packet report)."],
    )


def stock_ledger(db: Session, f: date, t: date, item_id: int | None = None) -> Report:
    items = [db.get(Item, item_id)] if item_id else db.scalars(
        select(Item).order_by(Item.kind, Item.code)).all()
    rows = []
    for it in items:
        bal = _sum(db, StockMove.qty, it.id, StockMove.move_date < f)
        rows.append({"date": f, "item": it.name, "type": "", "source": "Balance b/f",
                     "in": None, "out": None, "balance": bal, "rate": None, "value": None})
        moves = db.scalars(select(StockMove).where(
            StockMove.item_id == it.id, StockMove.move_date >= f, StockMove.move_date <= t)
            .order_by(StockMove.move_date, StockMove.id)).all()
        for m in moves:
            bal += m.qty
            rows.append({
                "date": m.move_date, "item": it.name, "type": m.move_type,
                "source": source_label(db, m),
                "in": m.qty if m.qty > 0 else None, "out": -m.qty if m.qty < 0 else None,
                "balance": bal, "rate": m.unit_cost, "value": m.value,
            })
    return Report(
        "stock-ledger", "Stock ledger",
        [("date", "Date"), ("item", "Item"), ("type", "Movement"), ("source", "Source document"),
         ("in", "In"), ("out", "Out"), ("balance", "Balance"), ("rate", "Rate"),
         ("value", "Value")],
        rows, f, t, notes=["Every row links to the document that caused it."],
    )


# ---------------------------------------------------------------- production

def production_report(db: Session, f: date, t: date) -> Report:
    rows = []
    runs = db.scalars(select(ProductionRun).where(
        ProductionRun.run_date >= f, ProductionRun.run_date <= t)
        .order_by(ProductionRun.run_date, ProductionRun.id)).all()
    for r in runs:
        s = run_summary(db, r)
        unit = {ln.item_id: db.get(Item, ln.item_id).stock_unit for ln in r.lines}
        rows.append({
            "date": r.run_date, "batch": r.batch_no, "line": f"YIELD: {r.product.name}",
            "unit": "pkt", "planned": s["expected_output"], "actual": s["actual_output"],
            "wasted": s["expected_output"] - s["actual_output"],
            "variance": s["yield_variance_pct"], "flag": "FLAG" if s["yield_flagged"] else "",
            "cost": None,
        })
        for ln in s["lines"]:
            rows.append({
                "date": r.run_date, "batch": r.batch_no, "line": ln["item"],
                "unit": unit[ln["item_id"]], "planned": ln["planned"], "actual": ln["actual"],
                "wasted": ln["wasted"], "variance": ln["variance_pct"],
                "flag": "FLAG" if ln["flagged"] else "", "cost": ln["cost"],
            })
    tol = run_summary(db, runs[0])["tolerance_pct"] if runs else None
    return Report(
        "production", "Production: planned vs actual",
        [("date", "Date"), ("batch", "Batch"), ("line", "Input / yield"), ("unit", "Unit"),
         ("planned", "Planned"), ("actual", "Actual"), ("wasted", "Wasted/short"),
         ("variance", "Var %"), ("flag", "Alert"), ("cost", "Cost (Rs)")],
        rows, f, t,
        notes=[f"Variance beyond +/-{tol}% is flagged." if tol is not None else "",
               "Packaging is planned on actual good output (a pouch is only needed for a "
               "packet that was made); recipe inputs are planned per batch."],
    )


def cost_per_packet(db: Session, f: date, t: date) -> Report:
    rows = []
    for c in costing.run_costs(db, f, t):
        rows.append({
            "row": c.batch_no, "product": c.product, "packets": c.good_packets,
            "material": c.material, "overhead": c.direct_overhead + c.pool_overhead,
            "total": c.total, "per_packet": c.per_packet, "price": None, "margin": None,
            "margin_pct": None,
        })
    for p in costing.product_costs(db, f, t):
        rows.append({
            "row": "PRODUCT", "product": p.product, "packets": p.good_packets,
            "material": p.material, "overhead": p.overhead, "total": p.material + p.overhead,
            "per_packet": p.per_packet, "price": p.sell_price, "margin": p.margin,
            "margin_pct": p.margin_pct,
        })
    return Report(
        "cost-per-packet", "Cost per packet and margin",
        [("row", "Run / product"), ("product", "Product"), ("packets", "Good packets"),
         ("material", "Material"), ("overhead", "Overheads"), ("total", "Total cost"),
         ("per_packet", "Cost/packet"), ("price", "Sell price"), ("margin", "Margin/packet"),
         ("margin_pct", "Margin %")],
        rows, f, t,
        notes=["Material at weighted-average cost of what was actually issued "
               "(wastage included).",
               "Overheads: direct to the run they are entered against; monthly pool shared "
               "by kg of good output."],
    )


# ---------------------------------------------------------------- receivables

def collections(db: Session, f: date, t: date) -> Report:
    pays = db.scalars(select(Payment).where(Payment.paid_on >= f, Payment.paid_on <= t)
                      .order_by(Payment.paid_on, Payment.id)).all()
    rows = [{
        "date": p.paid_on, "customer": db.get(Customer, p.customer_id).name,
        "against": db.get(Invoice, p.invoice_id).number if p.invoice_id else "On account",
        "mode": p.mode, "reference": p.reference or "", "amount": p.amount,
    } for p in pays]
    return Report(
        "collections", "Payments received",
        [("date", "Date"), ("customer", "Customer"), ("against", "Against"),
         ("mode", "Mode"), ("reference", "Reference"), ("amount", "Amount")],
        rows, f, t, total_keys=("amount",),
    )


AGE_BUCKETS = ((0, 30, "d0_30"), (31, 60, "d31_60"), (61, 90, "d61_90"), (91, 10**6, "d90"))


def open_invoices(db: Session, customer_id: int, as_of: date) -> list[tuple[Invoice, Decimal]]:
    """Unpaid part of each posted invoice. Payments against an invoice settle
    it; payments 'on account' settle the oldest invoices first."""
    invs = db.scalars(select(Invoice).where(
        Invoice.customer_id == customer_id, Invoice.status == "POSTED",
        Invoice.invoice_date <= as_of).order_by(Invoice.invoice_date, Invoice.id)).all()
    pays = db.scalars(select(Payment).where(
        Payment.customer_id == customer_id, Payment.paid_on <= as_of)).all()
    due = {i.id: i.total for i in invs}
    on_account = ZERO
    for p in pays:
        if p.invoice_id in due:
            due[p.invoice_id] -= p.amount
        else:
            on_account += p.amount
    out = []
    for i in invs:
        take = min(on_account, max(due[i.id], ZERO))
        due[i.id] -= take
        on_account -= take
        if due[i.id] > 0:
            out.append((i, due[i.id]))
    return out


def outstanding_ageing(db: Session, f: date, t: date) -> Report:
    rows = []
    for c in db.scalars(select(Customer).order_by(Customer.name)):
        row = {"customer": c.name, "gstin": c.gstin or "", **{b[2]: ZERO for b in AGE_BUCKETS}}
        for inv, amt in open_invoices(db, c.id, t):
            age = (t - inv.invoice_date).days
            for lo, hi, key in AGE_BUCKETS:
                if lo <= age <= hi:
                    row[key] += amt
        row["total"] = sum((row[b[2]] for b in AGE_BUCKETS), ZERO)
        rows.append(row)
    return Report(
        "outstanding", f"Outstanding receivables with ageing (as of {t:%d %b %Y})",
        [("customer", "Customer"), ("gstin", "GSTIN"), ("d0_30", "0-30 days"),
         ("d31_60", "31-60"), ("d61_90", "61-90"), ("d90", "90+"), ("total", "Outstanding")],
        rows, f, t, total_keys=("d0_30", "d31_60", "d61_90", "d90", "total"),
        notes=["Age is counted from the invoice date to the end of the period."],
    )


# ---------------------------------------------------------------- registry

REPORTS = {
    "sales-register": sales_register,
    "sales-by-customer": sales_by_customer,
    "sales-by-product": sales_by_product,
    "gst-summary": gst_summary,
    "gst-hsn": gst_hsn_summary,
    "purchase-register": purchase_register,
    "stock-summary": stock_summary,
    "stock-ledger": stock_ledger,
    "production": production_report,
    "cost-per-packet": cost_per_packet,
    "collections": collections,
    "outstanding": outstanding_ageing,
}
