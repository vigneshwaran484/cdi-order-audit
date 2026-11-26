"""Factory admin: verify orders, raise GST invoices, dispatch, record payments."""

import os
from collections.abc import Callable
from urllib.parse import quote

from itsdangerous import BadSignature, URLSafeSerializer

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.exports import invoice_pdf
from app.models import CreditNote, Customer, Invoice, Order, Payment, User
from app.services import sales
from app.services.reports import open_invoices
from app.web.common import STATES, back, day, dec, flash, get_db, render, role, settings

router = APIRouter()
# Signed link so the customer can open their invoice PDF without a login.
_share = URLSafeSerializer(os.environ.get("SESSION_SECRET", "dev-only-change-me"), salt="invoice")
admin_only = role("ADMIN")
can_view_invoices = role("ADMIN", "AUDITOR", "OWNER")


def act(request: Request, db: Session, fn: Callable, ok: str, url: str):
    """Run one business action in a transaction; show its error instead of a 500."""
    try:
        result = fn()
        db.commit()
    except ValueError as e:      # SalesError, InsufficientStock, UnitError
        db.rollback()
        flash(request, str(e), "error")
        return back(url)
    flash(request, ok.format(r=result))
    return result


# ---------------------------------------------------------------- orders

@router.get("/admin/orders")
def orders(request: Request, status: str = "SUBMITTED", user: User = Depends(admin_only),
           db: Session = Depends(get_db)):
    q = select(Order).order_by(Order.id.desc())
    if status != "ALL":
        q = q.where(Order.status == status)
    rows = []
    for o in db.scalars(q.limit(100)):
        check = sales.stock_check(db, o) if o.status == "SUBMITTED" else []
        rows.append((o, db.get(User, o.salesman_id), all(c.ok for c in check),
                     sales.tax_preview(db, o)))
    return render(request, "admin_orders.html", user, rows=rows, status=status)


@router.get("/admin/orders/{order_id}")
def order_detail(order_id: int, request: Request, user: User = Depends(admin_only),
                 db: Session = Depends(get_db)):
    o = db.get(Order, order_id)
    if o is None:
        return back("/admin/orders")
    invoice = db.scalar(select(Invoice).where(Invoice.order_id == o.id))
    return render(request, "admin_order.html", user, o=o, salesman=db.get(User, o.salesman_id),
                  check=sales.stock_check(db, o), tax=sales.tax_preview(db, o),
                  intra=sales.is_intra_state(db, o.customer), invoice=invoice)


@router.post("/admin/orders/{order_id}/edit")
async def order_edit(order_id: int, request: Request, user: User = Depends(admin_only),
                     db: Session = Depends(get_db)):
    form = await request.form()
    url = f"/admin/orders/{order_id}"

    def do():
        o = db.get(Order, order_id)
        lines = []
        for ln in o.lines:
            qty = dec(form.get(f"qty_{ln.id}"), "Quantity")
            if qty > 0:
                lines.append(sales.OrderLineIn(ln.item_id, qty, "pkt",
                                               dec(form.get(f"rate_{ln.id}"), "Rate")))
        return sales.edit_order(db, order_id=order_id, lines=lines, user_id=user.id)

    res = act(request, db, do, "Order updated (change logged in the audit trail)", url)
    return res if isinstance(res, Response) else back(url)


@router.post("/admin/orders/{order_id}/approve")
def order_approve(order_id: int, request: Request, invoice_now: str = Form(""),
                  invoice_date: str = Form(""), user: User = Depends(admin_only),
                  db: Session = Depends(get_db)):
    url = f"/admin/orders/{order_id}"
    res = act(request, db, lambda: sales.approve_order(db, order_id=order_id, user_id=user.id),
              "Order approved", url)
    if isinstance(res, Response):
        return res
    if invoice_now:
        inv = act(request, db, lambda: sales.create_invoice(
            db, order_id=order_id, invoice_date=day(invoice_date), user_id=user.id),
            "Invoice {r.number} raised", url)
        if not isinstance(inv, Response):
            return back(f"/admin/invoices/{inv.id}")
    return back(url)


@router.post("/admin/orders/{order_id}/reject")
def order_reject(order_id: int, request: Request, reason: str = Form(""),
                 user: User = Depends(admin_only), db: Session = Depends(get_db)):
    url = f"/admin/orders/{order_id}"
    res = act(request, db, lambda: sales.reject_order(
        db, order_id=order_id, reason=reason, user_id=user.id), "Order rejected", url)
    return res if isinstance(res, Response) else back(url)


@router.post("/admin/orders/{order_id}/invoice")
def order_invoice(order_id: int, request: Request, invoice_date: str = Form(""),
                  user: User = Depends(admin_only), db: Session = Depends(get_db)):
    url = f"/admin/orders/{order_id}"
    inv = act(request, db, lambda: sales.create_invoice(
        db, order_id=order_id, invoice_date=day(invoice_date), user_id=user.id),
        "Invoice {r.number} raised", url)
    return inv if isinstance(inv, Response) else back(f"/admin/invoices/{inv.id}")


# ---------------------------------------------------------------- invoices

@router.get("/admin/invoices")
def invoices(request: Request, user: User = Depends(can_view_invoices),
             db: Session = Depends(get_db)):
    rows = [(i, sales.invoice_balance(db, i))
            for i in db.scalars(select(Invoice).order_by(Invoice.id.desc()).limit(200))]
    return render(request, "admin_invoices.html", user, rows=rows)


@router.get("/admin/invoices/{invoice_id}")
def invoice_detail(invoice_id: int, request: Request, user: User = Depends(can_view_invoices),
                   db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    if inv is None:
        return back("/admin/invoices")
    pays = db.scalars(select(Payment).where(Payment.invoice_id == inv.id)).all()
    cn = db.scalar(select(CreditNote).where(CreditNote.invoice_id == inv.id))
    pdf_url = str(request.url_for("shared_invoice", token=_share.dumps(inv.id)))
    msg = (f"Invoice {inv.number} from {settings(db)['company_name']}: "
           f"Rs {inv.total:,.2f}. PDF: {pdf_url}")
    return render(request, "admin_invoice.html", user, inv=inv, pays=pays, cn=cn,
                  due=sales.invoice_balance(db, inv),
                  outstanding=sales.customer_outstanding(db, inv.customer_id),
                  whatsapp=f"https://wa.me/?text={quote(msg, safe='')}",
                  mailto=f"mailto:?subject={quote('Invoice ' + inv.number)}&body={quote(msg)}")


@router.get("/admin/invoices/{invoice_id}/pdf", name="invoice_pdf_file")
def invoice_pdf_file(invoice_id: int, user: User = Depends(can_view_invoices),
                     db: Session = Depends(get_db)):
    inv = db.get(Invoice, invoice_id)
    data = invoice_pdf(inv, settings(db), STATES)
    fname = inv.number.replace("/", "-") + ".pdf"
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


@router.get("/i/{token}", name="shared_invoice")
def shared_invoice(token: str, db: Session = Depends(get_db)):
    try:
        invoice_id = _share.loads(token)
    except BadSignature:
        return Response("Invalid link", status_code=404)
    inv = db.get(Invoice, invoice_id)
    data = invoice_pdf(inv, settings(db), STATES)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{inv.number.replace("/", "-")}.pdf"'})


@router.post("/admin/invoices/{invoice_id}/dispatch")
def invoice_dispatch(invoice_id: int, request: Request, dispatch_date: str = Form(""),
                     user: User = Depends(admin_only), db: Session = Depends(get_db)):
    url = f"/admin/invoices/{invoice_id}"
    res = act(request, db, lambda: sales.dispatch_invoice(
        db, invoice_id=invoice_id, dispatch_date=day(dispatch_date), user_id=user.id),
        "Dispatched: finished-goods stock reduced", url)
    return res if isinstance(res, Response) else back(url)


@router.post("/admin/invoices/{invoice_id}/cancel")
def invoice_cancel(invoice_id: int, request: Request, reason: str = Form(""),
                   user: User = Depends(admin_only), db: Session = Depends(get_db)):
    url = f"/admin/invoices/{invoice_id}"
    res = act(request, db, lambda: sales.cancel_invoice(
        db, invoice_id=invoice_id, reason=reason, note_date=day(None), user_id=user.id),
        "Cancelled with credit note {r.number}", url)
    return res if isinstance(res, Response) else back(url)


# ---------------------------------------------------------------- payments

@router.get("/admin/payments")
def payments(request: Request, customer_id: int | None = None,
             user: User = Depends(admin_only), db: Session = Depends(get_db)):
    customers = db.scalars(select(Customer).order_by(Customer.name)).all()
    balances = [(c, sales.customer_outstanding(db, c.id)) for c in customers]
    open_inv = open_invoices(db, customer_id, day(None)) if customer_id else []
    recent = db.scalars(select(Payment).order_by(Payment.id.desc()).limit(30)).all()
    return render(request, "admin_payments.html", user, balances=balances, customers=customers,
                  customer_id=customer_id, open_inv=open_inv, recent=recent,
                  names={c.id: c.name for c in customers},
                  inv_no={i.id: i.number for i in db.scalars(select(Invoice))})


@router.post("/admin/payments")
def payment_add(request: Request, customer_id: int = Form(...), amount: str = Form(...),
                mode: str = Form("UPI"), invoice_id: str = Form(""), paid_on: str = Form(""),
                reference: str = Form(""), user: User = Depends(admin_only),
                db: Session = Depends(get_db)):
    url = f"/admin/payments?customer_id={customer_id}"
    res = act(request, db, lambda: sales.record_payment(
        db, customer_id=customer_id, amount=dec(amount, "Amount"), paid_on=day(paid_on),
        mode=mode, invoice_id=int(invoice_id) if invoice_id else None,
        reference=reference or None, user_id=user.id),
        "Payment of Rs {r.amount} recorded", url)
    return res if isinstance(res, Response) else back(url)
