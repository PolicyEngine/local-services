"""Socket protocol versions this emitter accepts, and how it describes itself.

Builds start whichever emitter is installed, so a release must keep accepting
every protocol version that the `microcosm-emitter-interface` releases still in
use send. Remove a version only after no supported Microcosm release sends it.
"""

from typing import Final, TypedDict

SUPPORTED_PROTOCOL_VERSIONS: Final = (1,)


class EmitterDescription(TypedDict):
    """What `microcosm-emitter version` prints."""

    emitter_version: str
    protocol_versions: list[int]


class HelloResponse(EmitterDescription):
    """What a host started by `microcosm-emitter host` answers to `hello`.

    A host started with `python -m microcosm_emitter.host` has no description
    and refuses `hello` like any unknown action.
    """

    modules: list[str]
    skipped_modules: list[str]
