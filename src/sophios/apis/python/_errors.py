"""Internal exception types for the Python API."""


class InvalidInputValueError(Exception):
    pass


class MissingRequiredValueError(Exception):
    pass


class InvalidStepError(Exception):
    pass


class InvalidLinkError(Exception):
    pass


class InvalidCLTError(ValueError):
    pass
