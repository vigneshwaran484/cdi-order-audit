"""Salesman (mobile) pages.

The order screen works offline: it is cached by a service worker, the
customer and price list are embedded in the page, and a submitted order is
kept in the phone's localStorage until POST /api/orders succeeds. Each order
carries a client_ref (UUID made on the phone), so re-sending after a dropped
connection never creates a duplicate (see services/sales.submit_order).
"""

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Customer, Invoice, Item, Order, User
from app.services.sales import OrderLineIn, submit_order
from app.web.common import get_db, render, role

router = APIRouter()
salesman_only = role("SALESMAN")


def _my_customers(db: Session, user: User) -> list[Customer]:
    return db.scalars(select(Customer).where(
        (Customer.salesman_id == user.id) | Customer.salesman_id.is_(None))
        .order_by(Customer.name)).all()


@router.get("/m")
def order_screen(request: Request, user: User = Depends(salesman_only),
                 db: Session = Depends(get_db)):
    customers = [{"id": c.id, "name": c.name, "kind": c.kind} for c in _my_customers(db, user)]
    products = [{"id": p.id, "name": p.name, "price": str(p.sell_price), "gst": f"{p.gst_rate.normalize():f}"}
                for p in db.scalars(select(Item).where(Item.kind == "FINISHED").order_by(Item.name))]
    return render(request, "m_order.html", user, customers=customers, products=products)


@router.get("/m/orders")
def my_orders(request: Request, user: User = Depends(salesman_only),
              db: Session = Depends(get_db)):
    orders = db.scalars(select(Order).where(Order.salesman_id == user.id)
                        .order_by(Order.id.desc()).limit(50)).all()
    # Customer-wise sales = invoiced value of this salesman's orders.
    sales = db.execute(
        select(Customer.name, func.count(Invoice.id), func.coalesce(func.sum(Invoice.total), 0))
        .join(Invoice, Invoice.customer_id == Customer.id)
        .join(Order, Order.id == Invoice.order_id)
        .where(Order.salesman_id == user.id, Invoice.status == "POSTED")
        .group_by(Customer.name).order_by(Customer.name)
    ).all()
    return render(request, "m_orders.html", user, orders=orders, sales=sales)


class LineIn(BaseModel):
    item_id: int
    qty: Decimal = Field(gt=0)
    unit: str = "pkt"


class OrderIn(BaseModel):
    client_ref: str = Field(min_length=8, max_length=40)
    customer_id: int
    order_date: date | None = None
    lines: list[LineIn]


@router.post("/api/orders")
def api_submit(body: OrderIn, user: User = Depends(salesman_only),
               db: Session = Depends(get_db)):
    try:
        order = submit_order(
            db, salesman_id=user.id, customer_id=body.customer_id,
            client_ref=body.client_ref, order_date=body.order_date or date.today(),
            lines=[OrderLineIn(ln.item_id, ln.qty, ln.unit) for ln in body.lines],
        )
        db.commit()
    except ValueError as e:  # SalesError, UnitError
        db.rollback()
        return JSONResponse({"error": str(e)}, status_code=422)
    return {"id": order.id, "status": order.status, "client_ref": order.client_ref}
