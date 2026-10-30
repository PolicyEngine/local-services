"""Run repository quality guards."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.rules import file_length, locked_installs, uv_version  # noqa: E402
from scripts.rules import guard as rules_guard  # noqa: E402

Guard = tuple[str, Callable[[], list[str]]]

# Each guard needs a quality-guard rule in rules/registry.toml. The other
# checkers in scripts/rules (ci_scripts.check and pyright.check_coverage) are
# added here together with the rules that register them.
GUARDS: tuple[Guard, ...] = (
    ("rules-registry", rules_guard.check),
    ("file-length", file_length.check),
    ("locked-installs", locked_installs.check),
    ("uv-version", uv_version.check),
)


def main() -> int:
    """Run every guard, print its violations, and return 1 if any guard failed.

    All guards run even after one fails, so a single run reports every problem.
    """
    failed = False
    for name, check in GUARDS:
        violations = check()
        if not violations:
            print(f"{name}: passed")
            continue

        failed = True
        print(f"{name}: failed")
        for violation in violations:
            print(f"  - {violation}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
