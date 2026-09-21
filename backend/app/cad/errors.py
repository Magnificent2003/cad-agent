class CadError(ValueError):
    """A user-actionable CAD failure, safe to return through a worker boundary."""

    def __init__(self, message: str, code: str = "cad_error"):
        super().__init__(message)
        self.code = code

    def __reduce__(self):
        return (type(self), (str(self), self.code))
