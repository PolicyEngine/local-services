"""Client configuration; no database or hosted-service imports."""

from typing import Final

TELEMETRY_MODULE: Final = "telemetry"
DEFAULT_HEARTBEAT_SECONDS: Final = 60.0
SERVICE_START_WARNING: Final = "Local telemetry emitter service failed to start ({error_type}); the build will continue."
QUEUE_WARNING: Final = (
    "Telemetry could not be queued ({error_type}); the build will continue."
)
LOCAL_ONLY_REJECTED_EVENTS: Final = "collector_rejected_events"
REJECTED_EVENTS_MESSAGE: Final = (
    "Microcosm telemetry is local-only for this run: the collector rejected its "
    "events (HTTP {status}), so they will not be retried. The dataset build will "
    "continue."
)
