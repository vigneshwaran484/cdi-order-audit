from decimal import Decimal as D

import pytest

from app.units import UnitError, to_stock_unit


def test_mass_volume_and_carton_conversions(db, items):
    assert to_stock_unit(db, items["RM-OIL"], D("10500"), "ml") == D("10.5")
    assert to_stock_unit(db, items["RM-SPICE"], D("1200"), "g") == D("1.2")
    assert to_stock_unit(db, items["RM-FLOUR"], D("0.5"), "kg") == D("0.5")
    assert to_stock_unit(db, items["FG-MURUKKU-200"], D("4"), "ctn") == D("80")


def test_cross_dimension_is_refused(db, items):
    with pytest.raises(UnitError):
        to_stock_unit(db, items["RM-FLOUR"], D("1"), "l")
    with pytest.raises(UnitError):
        to_stock_unit(db, items["RM-FLOUR"], D("1"), "ctn")  # flour has no carton unit
