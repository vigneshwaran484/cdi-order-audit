"""Masters: items (raw, packaging, finished), BOM, customers, suppliers."""

from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    Item,
    ItemUnitConversion,
    PackLine,
    Recipe,
    Supplier,
    Unit,
    User,
)
from app.services import audit
from app.stock import balance
from app.web.admin import act
from app.web.common import back, dec, get_db, render, role

router = APIRouter()
viewers = role("PRODUCTION", "ADMIN", "OWNER")
editors = role("PRODUCTION", "ADMIN")
admin_only = role("ADMIN")


@router.get("/masters")
def masters(request: Request, user: User = Depends(viewers), db: Session = Depends(get_db)):
    items = db.scalars(select(Item).order_by(Item.kind, Item.code)).all()
    return render(
        request, "masters.html", user, items=items,
        bal={i.id: balance(db, i.id) for i in items},
        recipes=db.scalars(select(Recipe).order_by(Recipe.name)).all(),
        finished=[i for i in items if i.kind == "FINISHED"],
        packaging=[i for i in items if i.kind == "PACKAGING"],
        customers=db.scalars(select(Customer).order_by(Customer.name)).all(),
        suppliers=db.scalars(select(Supplier).order_by(Supplier.name)).all(),
        salesmen=db.scalars(select(User).where(User.role == "SALESMAN")).all(),
        units=db.scalars(select(Unit).order_by(Unit.dimension, Unit.code)).all(),
        users={u.id: u.full_name for u in db.scalars(select(User))},
        can_edit=user.role in ("PRODUCTION", "ADMIN"),
    )


def _code_free(db: Session, code: str) -> str:
    code = code.strip().upper()
    if not code:
        raise ValueError("Code is required")
    if db.scalar(select(Item).where(Item.code == code)):
        raise ValueError(f"Item code {code} already exists")
    return code


@router.post("/masters/items")
async def add_item(request: Request, user: User = Depends(editors), db: Session = Depends(get_db)):
    f = await request.form()

    def do():
        kind = f.get("kind")
        if kind not in ("RAW", "PACKAGING"):
            raise ValueError("Kind must be RAW or PACKAGING")
        item = Item(code=_code_free(db, f.get("code", "")), name=f.get("name", "").strip(),
                    kind=kind, stock_unit=f.get("stock_unit"),
                    reorder_level=dec(f.get("reorder_level"), "Reorder", Decimal("0")))
        db.add(item)
        db.flush()
        audit.log(db, user_id=user.id, entity="item", entity_id=item.id, action="CREATE",
                  after={"code": item.code, "name": item.name})
        return item

    res = act(request, db, do, "Item {r.code} added", "/masters")
    return res if isinstance(res, Response) else back("/masters")


@router.post("/masters/products")
async def add_product(request: Request, user: User = Depends(editors),
                      db: Session = Depends(get_db)):
    """A finished pack size. Several pack sizes share one recipe; expected
    packets per batch = recipe output (g) / pack weight (g)."""
    f = await request.form()

    def do():
        weight = dec(f.get("net_weight_g"), "Pack weight (g)")
        per_carton = dec(f.get("per_carton"), "Packets per carton", Decimal("0"))
        if weight <= 0:
            raise ValueError("Pack weight must be more than zero")
        product = Item(
            code=_code_free(db, f.get("code", "")), name=f.get("name", "").strip(),
            kind="FINISHED", stock_unit="pkt", hsn_code=f.get("hsn_code") or "21069099",
            gst_rate=dec(f.get("gst_rate"), "GST %", Decimal("5")),
            sell_price=dec(f.get("sell_price"), "Price"),
            recipe_id=int(f["recipe_id"]), net_weight_g=weight,
            reorder_level=dec(f.get("reorder_level"), "Reorder", Decimal("0")),
        )
        db.add(product)
        db.flush()
        if f.get("pouch_id"):
            product.pack_lines.append(PackLine(item_id=int(f["pouch_id"]), qty_per_packet=Decimal("1")))
        if f.get("carton_id") and per_carton > 0:
            product.pack_lines.append(PackLine(item_id=int(f["carton_id"]),
                                               qty_per_packet=Decimal("1") / per_carton))
            db.add(ItemUnitConversion(item_id=product.id, unit_code="ctn", factor=per_carton))
        db.flush()
        audit.log(db, user_id=user.id, entity="item", entity_id=product.id, action="CREATE",
                  after={"code": product.code, "recipe_id": product.recipe_id,
                         "net_weight_g": weight})
        return product

    res = act(request, db, do, "Product {r.name} added", "/masters")
    return res if isinstance(res, Response) else back("/masters")


@router.post("/masters/customers")
async def add_customer(request: Request, user: User = Depends(admin_only),
                       db: Session = Depends(get_db)):
    f = await request.form()

    def do():
        gstin = (f.get("gstin") or "").strip().upper() or None
        state = f.get("state_code", "33")
        if gstin and (len(gstin) != 15 or gstin[:2] != state):
            raise ValueError("GSTIN must be 15 characters and start with the state code")
        c = Customer(name=f.get("name", "").strip(), kind=f.get("kind", "RETAILER"),
                     gstin=gstin, state_code=state, address=f.get("address") or None,
                     salesman_id=int(f["salesman_id"]) if f.get("salesman_id") else None)
        if not c.name:
            raise ValueError("Name is required")
        db.add(c)
        db.flush()
        audit.log(db, user_id=user.id, entity="customer", entity_id=c.id, action="CREATE",
                  after={"name": c.name, "gstin": gstin})
        return c

    res = act(request, db, do, "Customer {r.name} added", "/masters#customers")
    return res if isinstance(res, Response) else back("/masters#customers")


@router.post("/masters/suppliers")
async def add_supplier(request: Request, user: User = Depends(editors),
                       db: Session = Depends(get_db)):
    f = await request.form()

    def do():
        s = Supplier(name=f.get("name", "").strip(), gstin=(f.get("gstin") or None),
                     state_code=f.get("state_code", "33"))
        if not s.name:
            raise ValueError("Name is required")
        db.add(s)
        db.flush()
        return s

    res = act(request, db, do, "Supplier {r.name} added", "/masters#suppliers")
    return res if isinstance(res, Response) else back("/masters#suppliers")
