"""Container start-up: create the tables and load the benchmark seed data
the first time only (an existing database is left alone).

To start again from the seed data:  docker compose exec app python -m app.seed
"""

import time

from sqlalchemy import inspect, select
from sqlalchemy.exc import OperationalError

from app.db import Base, SessionLocal, engine
from app.models import User
from app.seed import seed


def main() -> None:
    for _ in range(30):                      # wait for Postgres to accept connections
        try:
            with engine.connect():
                break
        except OperationalError:
            time.sleep(1)
    first_run = not inspect(engine).has_table("users")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        if first_run or db.scalar(select(User)) is None:
            seed(db)
            db.commit()
            print("Database created and benchmark seed data loaded.")
        else:
            print("Database already has data; seed skipped.")


if __name__ == "__main__":
    main()
