"""Sales loop: order -> verification -> GST invoice -> dispatch -> payment.

Status flow of an order:
    SUBMITTED --approve--> APPROVED --invoice--> INVOICED --dispatch--> DISPATCHED
        +--reject--> REJECTED            (cancel invoice -> credit note, CANCELLED)

Stock rules:
- Finished-goods stock goes DOWN only at dispatch (a DISPATCH ledger row per
  invoice line, source = the invoice), matching the problem statement.
- Between approval and dispatch the goods are COMMITTED: they are still on
  the shelf but promised. The stock check for a new order uses
      available = on hand - committed to other approved/invoiced orders
  so two orders cannot both be approved against the same packets.
- An order that is short is not approved; the admin sees the shortage and can
  reduce the quantity (edit), wait for production, or reject.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import gst
from app.models import (
    CreditNote,
    Customer,
    DocCounter,
    Invoice,
    InvoiceLine,
    Item,
    Order,
    OrderLine,
    Payment,
    Setting,
    StockMove,
    User,
)
from app.services import audit
from app.stock import InsufficientStock, balance, post_move
from app.units import to_stock_unit


class SalesError(ValueError):
    pass


@dataclass
class OrderLineIn:
    item_id: int
    qty: Decimal
    unit: str = "pkt"               # 'ctn' also works: 1 carton = 20 packets
    rate: Decimal | None = None     # None = take the price list rate


@dataclass
class StockCheckLine:
    item_id: int
    item: str
    ordered: Decimal
    on_hand: Decimal
    committed: Decimal      # promised to other approved, not yet dispatched orders
    available: Decimal
    shortage: Decimal

    @property
    def ok(self) -> bool:
        return self.shortage == 0


# ---------------------------------------------------------------- helpers

def _setting(db: Session, key: str) -> str:
    s = db.get(Setting, key)
    if s is None:
        raise SalesError(f"Setting '{key}' is missing")
    return s.value


def _next_number(db: Session, series: str, d: date) -> tuple[str, str]:
    """Next document number in this FY. Row lock keeps two clerks apart."""
    fy = gst.fin_year(d)
    counter = db.execute(
        select(DocCounter).where(DocCounter.series == series, DocCounter.fin_year == fy)
        .with_for_update()
    ).scalar_one_or_none()
    if counter is None:
        counter = DocCounter(series=series, fin_year=fy, last_no=0)
        db.add(counter)
    counter.last_no += 1
    db.flush()
    return f"{series}/{fy}/{counter.last_no:04d}", fy


def _order_snapshot(order: Order) -> dict:
    return {
        "status": order.status,
        "lines": [{"item_id": ln.item_id, "qty": ln.qty, "rate": ln.rate} for ln in order.lines],
    }


def _build_lines(db: Session, lines: list[OrderLineIn], allow_rate: bool) -> list[OrderLine]:
    if not lines:
        raise SalesError("An order needs at least one product")
    out: list[OrderLine] = []
    for ln in lines:
        item = db.get(Item, ln.item_id)
        if item is None or item.kind != "FINISHED":
            raise SalesError("Only finished products can be ordered")
        qty = to_stock_unit(db, item, Decimal(ln.qty), ln.unit)
        if qty <= 0 or qty != qty.to_integral_value():
            raise SalesError(f"{item.name}: quantity must be a whole number of packets")
        if ln.rate is not None and not allow_rate:
            raise SalesError("Salesmen cannot change prices; the price list applies")
        rate = Decimal(ln.rate) if ln.rate is not None else item.sell_price
        if rate is None or rate < 0:
            raise SalesError(f"{item.name} has no price")
        out.append(OrderLine(item_id=item.id, qty=qty, rate=rate))
    return out


# ---------------------------------------------------------------- orders

def submit_order(
    db: Session,
    *,
    salesman_id: int,
    customer_id: int,
    client_ref: str,
    order_date: date,
    lines: list[OrderLineIn],
) -> Order:
    """Salesman's order. `client_ref` is made on the phone, so an offline
    order that is sent twice when the network returns is stored only once."""
    existing = db.scalar(select(Order).where(Order.client_ref == client_ref))
    if existing is not None:
        return existing

    salesman = db.get(User, salesman_id)
    if salesman is None or salesman.role != "SALESMAN":
        raise SalesError("Only salesmen place orders")
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise SalesError("Unknown customer")
    if customer.salesman_id is not None and customer.salesman_id != salesman_id:
        raise SalesError("This customer belongs to another salesman")

    order = Order(client_ref=client_ref, customer_id=customer_id,
                  salesman_id=salesman_id, order_date=order_date, status="SUBMITTED")
    order.lines = _build_lines(db, lines, allow_rate=False)
    db.add(order)
    db.flush()
    audit.log(db, user_id=salesman_id, entity="order", entity_id=order.id,
              action="CREATE", after=_order_snapshot(order))
    return order


def committed_qty(db: Session, item_id: int, exclude_order_id: int | None = None) -> Decimal:
    """Packets promised to approved or invoiced orders not yet dispatched."""
    q = (select(func.coalesce(func.sum(OrderLine.qty), 0))
         .join(Order, Order.id == OrderLine.order_id)
         .where(OrderLine.item_id == item_id, Order.status.in_(("APPROVED", "INVOICED"))))
    if exclude_order_id is not None:
        q = q.where(Order.id != exclude_order_id)
    return Decimal(db.scalar(q))


def stock_check(db: Session, order: Order) -> list[StockCheckLine]:
    # Sum per product in case the same product is on two lines.
    wanted: dict[int, Decimal] = {}
    for ln in order.lines:
        wanted[ln.item_id] = wanted.get(ln.item_id, Decimal("0")) + ln.qty
    result = []
    for item_id, qty in wanted.items():
        on_hand = balance(db, item_id)
        committed = committed_qty(db, item_id, exclude_order_id=order.id)
        available = max(on_hand - committed, Decimal("0"))
        result.append(StockCheckLine(
            item_id=item_id, item=db.get(Item, item_id).name, ordered=qty,
            on_hand=on_hand, committed=committed, available=available,
            shortage=max(qty - available, Decimal("0")),
        ))
    return result


def is_intra_state(db: Session, customer: Customer) -> bool:
    return customer.state_code == _setting(db, "company_state_code")


def tax_preview(db: Session, order: Order) -> gst.LineTax:
    """What the invoice would come to (shown to the admin before approving)."""
    intra = is_intra_state(db, order.customer)
    return gst.sum_taxes([
        gst.line_tax(ln.qty, ln.rate, ln.item.gst_rate, intra) for ln in order.lines
    ])


def edit_order(db: Session, *, order_id: int, lines: list[OrderLineIn], user_id: int) -> Order:
    """Admin corrects quantities or prices before approval. Logged."""
    order = _get_order(db, order_id)
    if order.status != "SUBMITTED":
        raise SalesError(f"Order is {order.status}; only submitted orders can be edited")
    before = _order_snapshot(order)
    order.lines.clear()
    db.flush()
    order.lines.extend(_build_lines(db, lines, allow_rate=True))
    db.flush()
    audit.log(db, user_id=user_id, entity="order", entity_id=order.id,
              action="EDIT", before=before, after=_order_snapshot(order))
    return order


def approve_order(db: Session, *, order_id: int, user_id: int) -> Order:
    order = _get_order(db, order_id)
    if order.status != "SUBMITTED":
        raise SalesError(f"Order is {order.status}, not SUBMITTED")
    for line in stock_check(db, order):
        if not line.ok:
            raise InsufficientStock(line.item, line.ordered, line.available)
    order.status = "APPROVED"
    audit.log(db, user_id=user_id, entity="order", entity_id=order.id,
              action="APPROVE", before={"status": "SUBMITTED"}, after={"status": "APPROVED"})
    return order


def reject_order(db: Session, *, order_id: int, reason: str, user_id: int) -> Order:
    order = _get_order(db, order_id)
    if order.status != "SUBMITTED":
        raise SalesError(f"Order is {order.status}, not SUBMITTED")
    if not reason.strip():
        raise SalesError("Give a reason for rejecting")
    order.status = "REJECTED"
    order.reject_reason = reason.strip()
    audit.log(db, user_id=user_id, entity="order", entity_id=order.id,
              action="REJECT", after={"reason": order.reject_reason})
    return order


def _get_order(db: Session, order_id: int) -> Order:
    order = db.get(Order, order_id)
    if order is None:
        raise SalesError("Unknown order")
    return order


# ---------------------------------------------------------------- invoices

def create_invoice(db: Session, *, order_id: int, invoice_date: date, user_id: int) -> Invoice:
    order = _get_order(db, order_id)
    if order.status != "APPROVED":
        raise SalesError(f"Order is {order.status}; approve it before invoicing")

    customer = order.customer
    intra = is_intra_state(db, customer)
    number, fy = _next_number(db, _setting(db, "invoice_prefix"), invoice_date)

    invoice = Invoice(
        number=number, fin_year=fy, order_id=order.id, customer_id=customer.id,
        invoice_date=invoice_date, place_of_supply=customer.state_code,
        taxable=0, cgst=0, sgst=0, igst=0, total=0, status="POSTED", created_by=user_id,
    )
    taxes = []
    for ln in order.lines:
        t = gst.line_tax(ln.qty, ln.rate, ln.item.gst_rate, intra)
        taxes.append(t)
        invoice.lines.append(InvoiceLine(
            item_id=ln.item_id, hsn_code=ln.item.hsn_code, qty=ln.qty, rate=ln.rate,
            taxable=t.taxable, gst_rate=ln.item.gst_rate,
            cgst=t.cgst, sgst=t.sgst, igst=t.igst,
        ))
    total = gst.sum_taxes(taxes)
    invoice.taxable, invoice.cgst, invoice.sgst, invoice.igst = (
        total.taxable, total.cgst, total.sgst, total.igst)
    invoice.total = total.total
    db.add(invoice)
    order.status = "INVOICED"
    db.flush()
    audit.log(db, user_id=user_id, entity="invoice", entity_id=invoice.id, action="CREATE",
              after={"number": number, "total": invoice.total, "order_id": order.id})
    return invoice


def dispatch_invoice(db: Session, *, invoice_id: int, dispatch_date: date, user_id: int) -> Invoice:
    """Goods leave the factory: finished-goods stock goes down here."""
    invoice = _get_invoice(db, invoice_id)
    if invoice.status != "POSTED":
        raise SalesError("Cancelled invoices cannot be dispatched")
    if invoice.dispatched_at is not None:
        raise SalesError(f"{invoice.number} is already dispatched")
    for ln in invoice.lines:
        # Weighted-average cost of the packets leaving = cost of goods sold.
        post_move(db, item_id=ln.item_id, qty=-ln.qty, move_type="DISPATCH",
                  source_type="INVOICE", source_id=invoice.id, move_date=dispatch_date,
                  note=f"Dispatch {invoice.number}", user_id=user_id)
    invoice.dispatched_at = datetime.combine(dispatch_date, datetime.min.time(), timezone.utc)
    if invoice.order_id:
        db.get(Order, invoice.order_id).status = "DISPATCHED"
    audit.log(db, user_id=user_id, entity="invoice", entity_id=invoice.id,
              action="DISPATCH", after={"date": dispatch_date})
    return invoice


def cancel_invoice(db: Session, *, invoice_id: int, reason: str, note_date: date,
                   user_id: int) -> CreditNote:
    """Posted invoices are never deleted. A full credit note reverses the tax
    and the receivable; if the goods had left, they come back into stock at
    the same cost they went out at."""
    invoice = _get_invoice(db, invoice_id)
    if invoice.status != "POSTED":
        raise SalesError(f"{invoice.number} is already cancelled")
    if not reason.strip():
        raise SalesError("Give a reason for the credit note")

    number, _ = _next_number(db, "CN", note_date)
    note = CreditNote(
        number=number, invoice_id=invoice.id, note_date=note_date, reason=reason.strip(),
        taxable=invoice.taxable, cgst=invoice.cgst, sgst=invoice.sgst,
        igst=invoice.igst, total=invoice.total, created_by=user_id,
    )
    db.add(note)
    db.flush()

    if invoice.dispatched_at is not None:
        out_moves = db.scalars(select(StockMove).where(
            StockMove.source_type == "INVOICE", StockMove.source_id == invoice.id,
            StockMove.move_type == "DISPATCH")).all()
        for m in out_moves:
            post_move(db, item_id=m.item_id, qty=-m.qty, unit_cost=m.unit_cost,
                      value=-m.value, move_type="SALES_RETURN", source_type="CREDIT_NOTE",
                      source_id=note.id, move_date=note_date,
                      note=f"Return against {number}", user_id=user_id)

    invoice.status = "CANCELLED"
    if invoice.order_id:
        db.get(Order, invoice.order_id).status = "CANCELLED"
    audit.log(db, user_id=user_id, entity="invoice", entity_id=invoice.id, action="CANCEL",
              before={"status": "POSTED"},
              after={"status": "CANCELLED", "credit_note": number, "reason": note.reason})
    return note


def _get_invoice(db: Session, invoice_id: int) -> Invoice:
    invoice = db.get(Invoice, invoice_id)
    if invoice is None:
        raise SalesError("Unknown invoice")
    return invoice


# ---------------------------------------------------------------- payments

def invoice_balance(db: Session, invoice: Invoice) -> Decimal:
    if invoice.status == "CANCELLED":
        return Decimal("0.00")
    paid = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0))
                     .where(Payment.invoice_id == invoice.id))
    return invoice.total - Decimal(paid)


def customer_outstanding(db: Session, customer_id: int, as_of: date | None = None) -> Decimal:
    """Posted invoices minus all payments received from the customer.
    (A cancelled invoice is offset by its credit note, so it is left out.)"""
    inv = select(func.coalesce(func.sum(Invoice.total), 0)).where(
        Invoice.customer_id == customer_id, Invoice.status == "POSTED")
    pay = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.customer_id == customer_id)
    if as_of is not None:
        inv = inv.where(Invoice.invoice_date <= as_of)
        pay = pay.where(Payment.paid_on <= as_of)
    return Decimal(db.scalar(inv)) - Decimal(db.scalar(pay))


def record_payment(
    db: Session,
    *,
    customer_id: int,
    amount: Decimal,
    paid_on: date,
    mode: str,
    user_id: int,
    invoice_id: int | None = None,
    reference: str | None = None,
) -> Payment:
    amount = Decimal(amount)
    if amount <= 0:
        raise SalesError("Payment must be more than zero")
    if mode not in ("CASH", "UPI", "BANK", "CHEQUE"):
        raise SalesError("Mode must be CASH, UPI, BANK or CHEQUE")
    if invoice_id is not None:
        invoice = _get_invoice(db, invoice_id)
        if invoice.customer_id != customer_id:
            raise SalesError("That invoice belongs to another customer")
        due = invoice_balance(db, invoice)
        if amount > due:
            raise SalesError(f"{invoice.number} has only Rs {due} due")
    payment = Payment(customer_id=customer_id, invoice_id=invoice_id, amount=amount,
                      paid_on=paid_on, mode=mode, reference=reference, created_by=user_id)
    db.add(payment)
    db.flush()
    audit.log(db, user_id=user_id, entity="payment", entity_id=payment.id, action="CREATE",
              after={"customer_id": customer_id, "invoice_id": invoice_id, "amount": amount})
    return payment
