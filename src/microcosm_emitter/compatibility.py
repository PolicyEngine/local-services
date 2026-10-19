"""Socket protocol versions this emitter accepts.

Builds start whichever emitter is installed, so a release must keep accepting
every protocol version that the `microcosm-emitter-interface` releases still in
use send. Remove a version only after no supported Microcosm release sends it.
"""

from typing import Final

SUPPORTED_PROTOCOL_VERSIONS: Final = (1,)
