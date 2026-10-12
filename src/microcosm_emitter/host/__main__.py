"""Command-line entry for the generic service host."""

import argparse
import json
import sys
from pathlib import Path

from microcosm_emitter.host.constants import MAX_MESSAGE_BYTES
from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.host.loading import load_module
from microcosm_emitter.host.process import ParentProcess
from microcosm_emitter.host.runtime import ServiceRuntime


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-created-at", type=float, required=True)
    args = parser.parse_args()
    try:
        payload = sys.stdin.buffer.read(MAX_MESSAGE_BYTES + 1)
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValueError("configuration is too large")
        configurations = json.loads(payload)
        if not isinstance(configurations, dict) or not configurations:
            raise ValueError("module configurations must be a nonempty object")
        parent = ParentProcess(args.parent_pid, args.parent_created_at)
        context = ModuleContext(args.parent_pid)
        modules = {}
        for name, settings in configurations.items():
            if not isinstance(name, str) or not isinstance(settings, dict):
                raise ValueError("invalid module configuration")
            modules[name] = load_module(name, settings, context)
        ServiceRuntime(args.socket, modules=modules, is_parent_alive=parent.alive).run()
        return 0
    except Exception as error:
        # Never include configuration or module exception payloads in stderr.
        print(f"Local service failed ({type(error).__name__}).", file=sys.stderr)
        return 1
    finally:
        try:
            args.socket.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
