"""The `microcosm-emitter` command installed with `uv tool install microcosm-emitter`.

`microcosm-emitter host` runs the local service host. A build starts it as its
own child process, so the host inherits the build's environment variables and
user, including any Hugging Face credential. It is never a system service.
"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version

from microcosm_emitter.compatibility import (
    SUPPORTED_PROTOCOL_VERSIONS,
    EmitterDescription,
)

DISTRIBUTION_NAME = "microcosm-emitter"


def describe() -> EmitterDescription:
    """Versions a client checks before sending any hand-over."""
    return {
        "emitter_version": version(DISTRIBUTION_NAME),
        "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(prog="microcosm-emitter", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "host",
        help="Run the local service host; its configuration arrives on stdin.",
        add_help=False,
    )
    commands.add_parser(
        "version", help="Print the emitter and socket protocol versions as JSON."
    )
    selected, _ = parser.parse_known_args(arguments[:1])
    if selected.command == "host":
        from microcosm_emitter.host.__main__ import main as host_main

        return host_main(arguments[1:], description=describe())
    parser.parse_args(arguments)
    print(json.dumps(describe()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
