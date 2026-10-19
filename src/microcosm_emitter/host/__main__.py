"""Command-line entry for the generic service host."""

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from microcosm_emitter.host.constants import MAX_MESSAGE_BYTES
from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.host.errors import ModuleNotInstalledError, safe_startup_error
from microcosm_emitter.host.loading import load_module
from microcosm_emitter.host.process import parent_process
from microcosm_emitter.host.runtime import ServiceRuntime


def main(
    argv: list[str] | None = None,
    *,
    description: Mapping[str, Any] | None = None,
) -> int:
    """Run the host; ``description`` is what a ``hello`` request returns."""
    parser = argparse.ArgumentParser(prog="microcosm-emitter host")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    # Earlier launchers pass the parent's creation time. Without it, the host
    # reads it from its own parent, which must be the process named here.
    parser.add_argument("--parent-created-at", type=float)
    parser.add_argument(
        "--skip-unavailable-modules",
        action="store_true",
        help="Start without requested modules that this emitter does not install.",
    )
    args = parser.parse_args(argv)
    try:
        payload = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("configuration is too large")
        configurations = json.loads(payload)
        if not isinstance(configurations, dict) or not configurations:
            raise ValueError("module configurations must be a nonempty object")
        parent = parent_process(args.parent_pid, args.parent_created_at)
        context = ModuleContext(args.parent_pid)
        modules = {}
        skipped = []
        for name, settings in configurations.items():
            if not isinstance(name, str) or not isinstance(settings, dict):
                raise ValueError("invalid module configuration")
            try:
                modules[name] = load_module(name, settings, context)
            except ModuleNotInstalledError:
                if not args.skip_unavailable_modules:
                    raise
                skipped.append(name)
        if not modules:
            raise ValueError("no requested module is installed")
        ServiceRuntime(
            args.socket,
            modules=modules,
            is_parent_alive=parent.alive,
            description=(
                None
                if description is None
                else {
                    **description,
                    "modules": sorted(modules),
                    "skipped_modules": sorted(skipped),
                }
            ),
        ).run()
        return 0
    except Exception as error:
        # Never include configuration or module exception payloads in stderr.
        print(str(safe_startup_error(error)), file=sys.stderr)
        return 1
    finally:
        try:
            args.socket.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
