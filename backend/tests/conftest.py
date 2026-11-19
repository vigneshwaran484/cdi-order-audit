import os

# Point the app at the test database BEFORE app.db is imported.
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://cdi:cdi@localhost:5433/cdi_test"
)

import pytest
from sqlalchemy import select

from app.db import Base, SessionLocal, engine
from app.models import Item, Supplier
from app.seed import seed


@pytest.fixture
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    session = SessionLocal()
    seed(session)
    session.flush()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def items(db):
    """Look up seeded items by code: items['RM-FLOUR']."""
    return {i.code: i for i in db.scalars(select(Item))}


@pytest.fixture
def supplier(db):
    return db.scalar(select(Supplier))
