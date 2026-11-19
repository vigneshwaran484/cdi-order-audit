"""Unit conversion.

Two kinds of conversion:
1. Global, within one dimension: kg <-> g, l <-> ml (via Unit.to_base).
2. Item-specific: e.g. 1 'ctn' of Murukku 200 g = 20 'pkt'
   (via ItemUnitConversion, factor expressed in the item's stock unit).
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Item, ItemUnitConversion, Unit


class UnitError(ValueError):
    pass


def to_stock_unit(db: Session, item: Item, qty: Decimal, unit: str) -> Decimal:
    """Convert `qty` expressed in `unit` into the item's stock unit."""
    qty = Decimal(qty)
    if unit == item.stock_unit:
        return qty

    # Item-specific unit first (cartons of packets etc.)
    conv = db.get(ItemUnitConversion, (item.id, unit))
    if conv is not None:
        return qty * conv.factor

    src = db.get(Unit, unit)
    dst = db.get(Unit, item.stock_unit)
    if src is None or dst is None:
        raise UnitError(f"Unknown unit '{unit}' for {item.name}")
    if src.dimension != dst.dimension:
        raise UnitError(
            f"Cannot convert {src.dimension} '{unit}' to {dst.dimension} "
            f"'{item.stock_unit}' for {item.name}"
        )
    return qty * src.to_base / dst.to_base
