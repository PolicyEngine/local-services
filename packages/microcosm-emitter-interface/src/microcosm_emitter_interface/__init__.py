"""Typed hand-overs from Microcosm builds to the separately installed emitter.

Importing this package starts no process and opens no socket or file.
"""

from microcosm_emitter_interface.client import EmitterClient, default_spool_path
from microcosm_emitter_interface.launcher import (
    COMMAND_ENVIRONMENT_VARIABLE,
    DEFAULT_COMMAND,
    EmitterStartupError,
)
from microcosm_emitter_interface.orrery import OrrerySettings
from microcosm_emitter_interface.protocol import PROTOCOL_VERSION
from microcosm_emitter_interface.telemetry import TelemetryRun, TelemetrySettings

__all__ = [
    "COMMAND_ENVIRONMENT_VARIABLE",
    "DEFAULT_COMMAND",
    "PROTOCOL_VERSION",
    "EmitterClient",
    "EmitterStartupError",
    "OrrerySettings",
    "TelemetryRun",
    "TelemetrySettings",
    "default_spool_path",
]
