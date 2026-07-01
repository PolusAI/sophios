"""Public API namespace for Sophios."""

from importlib import import_module
from types import ModuleType


__all__ = ["python", "rest", "utils"]


def __getattr__(name: str) -> ModuleType:
    if name in __all__:
        return import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
