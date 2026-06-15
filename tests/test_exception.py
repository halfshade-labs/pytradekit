import pytest
from pytradekit.utils.exceptions import JwjException, NoDataException, ExchangeException, DataTypeException, MinNotionalException, LotSizeException


def test_jwj_exception():
    with pytest.raises(JwjException) as excinfo:
        raise JwjException("This is a base exception")
    assert str(excinfo.value) == "This is a base exception"


def test_no_data_exception():
    with pytest.raises(NoDataException) as excinfo:
        raise NoDataException(note="Missing data details")
    assert str(excinfo.value) == "no data for next action"
    assert excinfo.value.note == "Missing data details"


def test_exchange_exception():
    with pytest.raises(ExchangeException) as excinfo:
        raise ExchangeException(note="Connection lost")
    assert str(excinfo.value) == "exchange connect error"
    assert excinfo.value.note == "Connection lost"


def test_data_type_exception():
    with pytest.raises(DataTypeException) as excinfo:
        raise DataTypeException(note="Expected int but got str")
    assert str(excinfo.value) == "data type is not as except"
    assert excinfo.value.note == "Expected int but got str"


def test_min_notional_exception():
    with pytest.raises(MinNotionalException) as excinfo:
        raise MinNotionalException(note="qty too small")
    assert str(excinfo.value) == "trade amount below minimum notional value"
    assert excinfo.value.note == "qty too small"


def test_lot_size_exception():
    with pytest.raises(LotSizeException) as excinfo:
        raise LotSizeException(note="qty not aligned to step")
    assert str(excinfo.value) == "order quantity not aligned to lot size step"
    assert excinfo.value.note == "qty not aligned to step"
    assert isinstance(excinfo.value, JwjException)
