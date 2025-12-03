class JwjException(Exception):
    """
      JWJ project base exception. Handled at the outermost level.
      All other exception types are subclasses of this exception type.
    """


class NoDataException(JwjException):
    def __init__(self, note=None):
        super().__init__('no data for next action')
        self.note = note


class ExchangeException(JwjException):
    def __init__(self, note=None):
        super().__init__('exchange connect error')
        self.note = note


class DataTypeException(JwjException):
    def __init__(self, note=None):
        super().__init__('data type is not as except')
        self.note = note


class DependencyException(JwjException):
    def __init__(self, note=None):
        super().__init__('dependency is not work well')
        self.note = note


class MethodNotImplementedError(JwjException):
    def __init__(self, note=None):
        super().__init__('no method in class error')
        self.note = note


class InsufficientBalanceException(JwjException):
    def __init__(self, note=None):
        super().__init__('insufficient balance')
        self.note = note


class MinNotionalException(JwjException):
    def __init__(self, note=None):
        super().__init__('trade amount below minimum notional value')
        self.note = note


class PlaceOrderException(JwjException):
    def __init__(self, note=None):
        super().__init__('place order exception')
        self.note = note


class CancelOrderException(JwjException):
    def __init__(self, note=None):
        super().__init__('cancel order exception')
        self.note = note
