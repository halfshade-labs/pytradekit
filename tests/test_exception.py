import pytest
from pytradekit.utils.exceptions import JwjException, NoDataException, ExchangeException, DataTypeException


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
