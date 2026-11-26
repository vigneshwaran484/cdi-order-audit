"""FastAPI app: server-rendered pages (Jinja2) plus a small JSON API for the
salesman's offline-capable order screen.

Run:  uv run uvicorn app.main:app --reload
"""

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.models import User
from app.security import verify_password
from app.web import admin, masters, production, reports, salesman
from app.web.common import HOME, LoginRequired, back, current_user, get_db, render

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Order-to-Audit", docs_url="/api/docs")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-only-change-me"),
    same_site="lax",
    max_age=60 * 60 * 24 * 30,  # salesmen stay logged in on their phone for a month
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

for module in (salesman, admin, production, masters, reports):
    app.include_router(module.router)


@app.exception_handler(LoginRequired)
async def to_login(request: Request, exc: LoginRequired):
    if request.url.path.startswith("/api/"):
        return HTMLResponse("Login required", status_code=401)
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    return HTMLResponse(
        f"<h2>{exc.status_code}</h2><p>{exc.detail}</p><p><a href='/'>Home</a></p>",
        status_code=exc.status_code,
    )


@app.get("/sw.js")
def service_worker():
    # Served from the root so it can control /m (the salesman pages) offline.
    return FileResponse(STATIC / "sw.js", media_type="application/javascript")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/")
def home(user: User = Depends(current_user)):
    return back(HOME[user.role])


@app.get("/login")
def login_page(request: Request):
    return render(request, "login.html", None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None or not verify_password(password, user.password_hash):
        return render(request, "login.html", None, error="Wrong username or password",
                      username=username)
    request.session.clear()
    request.session["uid"] = user.id
    return back(HOME[user.role])


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")
