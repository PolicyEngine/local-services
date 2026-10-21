"""The socket protocol between a build and its separately installed emitter.

``PROTOCOL_VERSION`` names the shape of every message this package sends. An
emitter lists the versions it accepts in its ``hello`` response; this package
refuses an emitter that does not list its version. Adding a module or a message
kind keeps the version. Changing an existing message's shape needs a new version,
and every emitter release keeps accepting every version still in use.
"""

from typing import Final, Literal

PROTOCOL_VERSION: Final = 1

DELIMITER: Final = b"\n"
ACK_OK: Final = b"ok\n"
ACK_ERROR: Final = b"error\n"
MAX_MESSAGE_BYTES: Final = 1_048_576

TELEMETRY_MODULE: Final = "telemetry"
ORRERY_MODULE: Final = "orrery"
type ModuleName = Literal["telemetry", "orrery"]

#: Telemetry requests only wait for the service to commit their events.
ACKNOWLEDGEMENT_TIMEOUT_SECONDS: Final = 0.2
#: Orrery requests hash a bounded set of preserved files before answering.
FILE_REQUEST_TIMEOUT_SECONDS: Final = 30.0
STARTUP_TIMEOUT_SECONDS: Final = 15.0
STARTUP_POLL_SECONDS: Final = 0.02
DRAIN_SECONDS: Final = 15.0
PROCESS_EXIT_TIMEOUT_SECONDS: Final = 1.0
RUNTIME_PREFIX: Final = "pe-service-"
SOCKET_FILENAME: Final = "service.sock"
