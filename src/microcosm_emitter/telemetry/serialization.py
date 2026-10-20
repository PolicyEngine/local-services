"""Convert Python request arguments to JSON without interpreting telemetry.

The conversion is the interface package's, so the in-process client and
``microcosm_emitter_interface`` send identical messages.
"""

from microcosm_emitter_interface.serialization import wire_value as wire_value
