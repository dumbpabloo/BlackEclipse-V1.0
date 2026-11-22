"""Named failures for the plugin kernel.

Every rejection is a stable code plus a message a human can act on. The kernel
never raises a bare ``KeyError``/``RuntimeError`` across a public boundary, and
a plugin failure never takes down the process: it becomes a ``FAILED`` fiber with
its original error attached.
"""

from __future__ import annotations

from typing import Any, Optional


class KernelError(Exception):
    """Base class for every kernel rejection."""

    def __init__(self, code: str, message: str, *, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ServiceUnavailable(KernelError):
    def __init__(self, key: str, *, waiting_on: Optional[list[str]] = None) -> None:
        super().__init__(
            "KERNEL_SERVICE_UNAVAILABLE",
            f"required service {key!r} is not provided",
            detail={"service": key, "missing": waiting_on or [key]},
        )


class ServiceConflict(KernelError):
    def __init__(self, key: str) -> None:
        super().__init__(
            "KERNEL_SERVICE_CONFLICT",
            f"service {key!r} is already provided; replacement requires replace=True",
            detail={"service": key},
        )


class PluginRejected(KernelError):
    def __init__(self, code: str, message: str, *, plugin: str = "") -> None:
        super().__init__(code, message, detail={"plugin": plugin})


class CompositionError(KernelError):
    def __init__(self, code: str, message: str, *, source: str = "", row: str = "") -> None:
        super().__init__(code, message, detail={"source": source, "row": row})


class SupervisorError(KernelError):
    def __init__(self, code: str, message: str, *, row: str = "", rollback: str = "") -> None:
        super().__init__(code, message, detail={"row": row, "rollback": rollback})
