"""Socket protocol versions this emitter accepts, and how it describes itself.

Builds start whichever emitter is installed, so a release must keep accepting
every protocol version that the `microcosm-emitter-interface` releases still in
use send. Remove a version only after no supported Microcosm release sends it.
"""

from typing import Final, TypedDict

SUPPORTED_PROTOCOL_VERSIONS: Final = (1,)


class EmitterDescription(TypedDict):
    """The versions a client checks before sending any hand-over."""

    emitter_version: str
    protocol_versions: list[int]


class VersionReport(EmitterDescription):
    """What `microcosm-emitter version` prints.

    ``collector_origin`` is the collector this emitter sends to when no module
    setting chooses another trusted origin. The Modal runner compares it with
    the origin a collector token was obtained for.
    """

    collector_origin: str


class HelloResponse(EmitterDescription):
    """What a host started by `microcosm-emitter host` answers to `hello`.

    A host started with `python -m microcosm_emitter.host` has no description
    and refuses `hello` like any unknown action.
    """

    modules: list[str]
    skipped_modules: list[str]
