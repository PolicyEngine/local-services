"""Verify clean, non-editable installs away from the source checkout.

Two environments mirror production: the emitter's own, as
`uv tool install microcosm-emitter` creates it, and a build's, which holds only
the interface package and starts the emitter's command as its child.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EMITTER_PREFIX = "microcosm_emitter-"
INTERFACE_PREFIX = "microcosm_emitter_interface-"


def run(*arguments, **kwargs):
    subprocess.run(arguments, check=True, **kwargs)


def only(paths: list[Path], prefix: str) -> Path:
    matches = [path for path in paths if path.name.startswith(prefix)]
    assert len(matches) == 1, f"Expected one {prefix}* artifact, found {matches}"
    return matches[0]


def create_environment(directory: Path, *wheels: Path) -> Path:
    run("uv", "venv", "--python", sys.executable, str(directory))
    interpreter = directory / "bin/python"
    run("uv", "pip", "install", "--python", str(interpreter), *map(str, wheels))
    return interpreter


def main():
    with tempfile.TemporaryDirectory(prefix="pe-wheel-") as directory:
        workspace = Path(directory)
        wheels = list((ROOT / "dist").glob("*.whl"))
        sources = list((ROOT / "dist").glob("*.tar.gz"))
        assert len(wheels) == len(sources) == 2, (
            "Expected an emitter and an interface wheel and source archive"
        )
        for prefix in (EMITTER_PREFIX, INTERFACE_PREFIX):
            only(sources, prefix)
        emitter_wheel = only(wheels, EMITTER_PREFIX)
        interface_wheel = only(wheels, INTERFACE_PREFIX)
        emitter_interpreter = create_environment(
            workspace / "emitter-tool", emitter_wheel, interface_wheel
        )
        build_interpreter = create_environment(workspace / "build", interface_wheel)
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "HF_TOKEN",
                "HUGGINGFACE_TOKEN",
                "HUGGING_FACE_HUB_TOKEN",
                "PYTHONPATH",
                "MICROCOSM_EMITTER_COMMAND",
            }
        }
        environment.update(
            HF_HOME=str(workspace / "hf"), HF_TOKEN_PATH=str(workspace / "hf/token")
        )
        run(
            str(emitter_interpreter),
            "-I",
            str(ROOT / "tools/installed-smoke.py"),
            cwd=workspace,
            env=environment,
        )
        run(
            str(build_interpreter),
            "-I",
            str(ROOT / "tools/installed-interface-smoke.py"),
            str(workspace / "emitter-tool/bin/microcosm-emitter"),
            cwd=workspace,
            env=environment,
        )


if __name__ == "__main__":
    main()
