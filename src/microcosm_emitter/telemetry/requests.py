"""Commands sent over the local socket, not the hosted collector's event API."""

from typing import Final

from microcosm_emitter_interface.telemetry import (
    DEFAULT_FAILURE_CLASS as DEFAULT_FAILURE_CLASS,
)

COMMAND_EMIT: Final = "emit"
COMMAND_STAGE: Final = "stage"
COMMAND_TRANSITION_STAGE: Final = "transition_stage"
COMMAND_PROGRESS: Final = "progress"
COMMAND_CALIBRATION_PROGRESS: Final = "calibration_progress"
COMMAND_TRANSITION_CALIBRATION_PROGRESS: Final = "transition_calibration_progress"
COMMAND_FAIL: Final = "fail"
COMMAND_BLOCK: Final = "block"
COMMAND_COMPLETE: Final = "complete"
