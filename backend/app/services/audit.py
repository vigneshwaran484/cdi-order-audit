"""Audit trail helper: call this whenever a document is created or changed."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import AuditLog


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def log(
    db: Session,
    *,
    user_id: int | None,
    entity: str,
    entity_id: int,
    action: str,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    db.add(AuditLog(
        user_id=user_id,
        entity=entity,
        entity_id=entity_id,
        action=action,
        before=_jsonable(before) if before else None,
        after=_jsonable(after) if after else None,
    ))
