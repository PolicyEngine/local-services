"""Explicit failures; callers decide whether their work can continue."""

import builtins
from typing import Literal


class LocalServiceError(RuntimeError):
    """A local service could not start, accept a message, or shut down."""


StartupFailureReason = Literal[
    "readiness_timeout",
    "configuration_timeout",
    "child_exit",
    "invalid_configuration",
    "startup_failure",
]

_STARTUP_MESSAGES = {
    "readiness_timeout": "Service readiness timed out",
    "configuration_timeout": "Service configuration transfer timed out",
    "child_exit": "Service exited before readiness",
    "invalid_configuration": "Service configuration is invalid or too large",
    "startup_failure": "Service startup failed",
}


class ServiceStartupError(LocalServiceError):
    """Fixed startup reason and validated metadata, never raw exception details."""

    def __init__(
        self,
        reason: StartupFailureReason,
        *,
        exit_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        if reason not in _STARTUP_MESSAGES:
            raise ValueError("Unknown startup failure reason.")
        self.reason = reason
        self.exit_code = exit_code if type(exit_code) is int else None
        builtin = (
            getattr(builtins, error_type, None) if isinstance(error_type, str) else None
        )
        self.error_type = (
            error_type
            if isinstance(builtin, type) and issubclass(builtin, Exception)
            else "Exception"
            if error_type is not None
            else None
        )
        metadata = [reason]
        if self.exit_code is not None:
            metadata.append(f"exit_code={self.exit_code}")
        if self.error_type is not None:
            metadata.append(self.error_type)
        super().__init__(f"{_STARTUP_MESSAGES[reason]} ({', '.join(metadata)}).")


def safe_startup_error(
    error: Exception, *, reason: StartupFailureReason = "startup_failure"
) -> ServiceStartupError:
    """Copy only safe metadata, without the original traceback, cause or context."""
    if isinstance(error, ServiceStartupError):
        return ServiceStartupError(
            error.reason, exit_code=error.exit_code, error_type=error.error_type
        )
    error_class = type(error)
    error_type = (
        error_class.__name__
        if getattr(builtins, error_class.__name__, None) is error_class
        else "Exception"
    )
    return ServiceStartupError(reason, error_type=error_type)
