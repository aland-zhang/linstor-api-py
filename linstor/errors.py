class LinstorError(Exception):
    """
    Linstor basic error class with a message
    """
    def __init__(self, msg, more_errors=None):
        self._msg = msg
        if more_errors is None:
            more_errors = []
        self._errors = more_errors

    def all_errors(self):
        return self._errors

    @property
    def message(self):
        return self._msg

    def __str__(self):
        return "Error: {msg}".format(msg=self._msg)

    def __repr__(self):
        return "LinstorError('{msg}')".format(msg=self._msg)


class LinstorNetworkError(LinstorError):
    """
    Linstor Error indicating an network/connection error.
    """
    def __init__(self, msg, more_errors=None):
        super(LinstorNetworkError, self).__init__(msg, more_errors)


class LinstorTimeoutError(LinstorError):
    """
    Linstor network timeout error
    """
    def __init__(self, msg, more_errors=None):
        super(LinstorTimeoutError, self).__init__(msg, more_errors)