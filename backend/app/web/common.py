"""Shared pieces for the web routes: DB session, login, role checks, flash messages.

Role-based access: every route declares the roles allowed with
`Depends(role("ADMIN", ...))`. A user with another role gets 403, and a
logged-out user is sent to the login page.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Setting, User

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

HOME = {
    "SALESMAN": "/m",
    "ADMIN": "/admin/orders",
    "PRODUCTION": "/production",
    "AUDITOR": "/reports",
    "OWNER": "/dashboard",
}

# What each role sees in the top menu (and may open).
MENU = [
    ("Dashboard", "/dashboard", {"OWNER", "ADMIN"}),
    ("Orders", "/admin/orders", {"ADMIN"}),
    ("Invoices", "/admin/invoices", {"ADMIN", "AUDITOR", "OWNER"}),
    ("Payments", "/admin/payments", {"ADMIN"}),
    ("Production", "/production", {"PRODUCTION", "ADMIN"}),
    ("Purchases", "/production/purchases", {"PRODUCTION", "ADMIN"}),
    ("Stock", "/stock", {"PRODUCTION", "ADMIN", "AUDITOR", "OWNER"}),
    ("Masters", "/masters", {"PRODUCTION", "ADMIN", "OWNER"}),
    ("Reports", "/reports", {"ADMIN", "AUDITOR", "OWNER"}),
    ("Audit log", "/audit-log", {"ADMIN", "AUDITOR", "OWNER"}),
]


class LoginRequired(Exception):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None:
        raise LoginRequired()
    return user


def role(*roles: str):
    def check(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, f"Your role ({user.role}) cannot open this page")
        return user
    return check


def settings(db: Session) -> dict:
    return {s.key: s.value for s in db.query(Setting)}


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def render(request: Request, name: str, user: User | None, **ctx):
    msgs = request.session.pop("flash", [])
    menu = [(label, href) for label, href, roles in MENU if user and user.role in roles]
    return TEMPLATES.TemplateResponse(request, name, {
        "user": user, "menu": menu, "flashes": msgs, "path": request.url.path, **ctx})


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def dec(value: str | None, field: str = "value", default: Decimal | None = None) -> Decimal:
    if value is None or str(value).strip() == "":
        if default is not None:
            return default
        raise ValueError(f"{field} is required")
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{field}: '{value}' is not a number")


def day(value: str | None) -> date:
    return date.fromisoformat(value) if value else date.today()


STATES = {
    "29": "Karnataka", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "36": "Telangana", "37": "Andhra Pradesh", "27": "Maharashtra", "07": "Delhi",
}


def fmt_money(v) -> str:
    if v is None or v == "":
        return ""
    v = Decimal(v)
    sign = "-" if v < 0 else ""
    whole, frac = f"{abs(v):.2f}".split(".")
    # Indian grouping: 12,34,567.00
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{whole}.{frac}"


def fmt_qty(v) -> str:
    if v is None or v == "":
        return ""
    if not isinstance(v, Decimal):
        return str(v)
    return f"{v.normalize():f}" if v == v.to_integral_value() else f"{v.normalize():f}"


TEMPLATES.env.filters["money"] = fmt_money
TEMPLATES.env.filters["qty"] = fmt_qty
TEMPLATES.env.globals["STATES"] = STATES


# Report cells: quantities print as plain numbers, percentages and unit
# rates as stored, everything else numeric as money.
QTY_KEYS = {"qty", "packets", "opening", "receipts", "issues", "adjust", "closing", "in",
            "out", "balance", "planned", "actual", "wasted", "invoices", "count"}
PLAIN_KEYS = {"variance", "margin_pct", "rate", "pos"}


def fmt_cell(v, key: str) -> str:
    if v is None:
        return ""
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, (Decimal, int)) and not isinstance(v, bool):
        if key in QTY_KEYS:
            return fmt_qty(Decimal(v))
        if key in PLAIN_KEYS:
            return str(v)
        return fmt_money(v)
    return str(v)


TEMPLATES.env.filters["cell"] = fmt_cell
