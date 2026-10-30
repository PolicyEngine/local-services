"""The ruff-ignores quality guard: Ruff ignores only what the registry lists.

With every Ruff rule selected (``ALL``), the root ``pyproject.toml`` switches
some off. Each selector it ignores, in ``[tool.ruff.lint]`` or ``[tool.ruff]``
``ignore`` or ``extend-ignore``, must be listed by the rule this guard
enforces, in one of two ways:

- in its baseline: a rule family whose findings are not fixed yet. The rule
  is ratcheted, so the guard refuses an entry Ruff no longer ignores, and,
  compared with the base branch (``compare_with_base``), any family the base
  branch's baseline does not list: the list may only lose entries;
- as one of its ``[[rule.exception]]`` entries on ``pyproject.toml``, whose
  ``match`` is the selector: a rule that stays off, such as one that
  conflicts with ruff format, with its reason. Compared with the base branch,
  no exception may be added.

Ruff can switch rules off in other ways too, and the guard refuses each one
that no exception names: ``extend-per-file-ignores`` (``per-file-ignores``
entries are described by registered ruff per-file-ignore rules instead), a
path in ``exclude`` or ``extend-exclude``, which needs an exception whose
``match`` is the path, and another Ruff configuration (``ruff.toml``,
``.ruff.toml``, or a ``pyproject.toml`` with ``[tool.ruff]`` below the root),
which Ruff reads in place of the root's for the files it covers and which
needs an exception whose ``path`` is that file.

It reads the configuration only and does not run Ruff, so a test with Ruff
installed checks that each listed family still has findings.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from scripts.rules.baseline import baseline_lines
from scripts.rules.load import load_registry
from scripts.rules.locked_installs import tracked_files
from scripts.rules.model import Kind, Registry, Rule, Violation

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_NAME = "ruff-ignores"
ROOT_PROJECT = "pyproject.toml"
RUFF_FILES = frozenset({"ruff.toml", ".ruff.toml"})


def guard_rule(registry: Registry) -> Rule | None:
    """Return the rule this guard enforces, if the registry has one."""
    return next(
        (
            rule
            for rule in registry.rules
            if rule.enforcer.kind is Kind.QUALITY_GUARD
            and rule.enforcer.get("name") == GUARD_NAME
        ),
        None,
    )


def _toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _ruff_settings(root: Path) -> dict[str, Any]:
    """Return the root pyproject.toml's ``[tool.ruff]`` table."""
    return _toml(root / ROOT_PROJECT).get("tool", {}).get("ruff", {})


def ignored_selectors(ruff: dict[str, Any]) -> list[str]:
    """Return what ``[tool.ruff.lint]`` and ``[tool.ruff]`` ignore, in order."""
    lint = ruff.get("lint", {})
    return [
        *lint.get("ignore", []),
        *lint.get("extend-ignore", []),
        *ruff.get("ignore", []),
        *ruff.get("extend-ignore", []),
    ]


def _excluded(ruff: dict[str, Any]) -> list[str]:
    """Return the paths Ruff's ``exclude`` and ``extend-exclude`` leave out."""
    lint = ruff.get("lint", {})
    return [
        *ruff.get("exclude", []),
        *ruff.get("extend-exclude", []),
        *lint.get("exclude", []),
    ]


def _extend_per_file_ignores(ruff: dict[str, Any]) -> list[str]:
    """Return the file patterns of Ruff's ``extend-per-file-ignores`` tables."""
    lint = ruff.get("lint", {})
    return [
        *lint.get("extend-per-file-ignores", {}),
        *ruff.get("extend-per-file-ignores", {}),
    ]


def other_configurations(root: Path, files: Iterable[str]) -> list[str]:
    """Return the tracked Ruff configurations besides the root pyproject.toml."""
    found = []
    for path in sorted(files):
        name = PurePosixPath(path).name
        if name in RUFF_FILES:
            found.append(path)
        elif (
            name == ROOT_PROJECT
            and path != ROOT_PROJECT
            and "ruff" in _toml(root / path).get("tool", {})
        ):
            found.append(path)
    return found


def _baseline(root: Path, rule: Rule) -> list[str]:
    """Return the selectors the rule's baseline lists."""
    if rule.baseline is None or not (root / rule.baseline.file).is_file():
        return []
    return baseline_lines((root / rule.baseline.file).read_text())


def find_violations(root: Path, rule: Rule) -> list[Violation]:
    """Compare what Ruff ignores with what ``rule`` lists."""

    def violation(message: str, fix: str) -> Violation:
        return Violation(rule.id, GUARD_NAME, message, fix)

    ruff = _ruff_settings(root)
    ignored = ignored_selectors(ruff)
    excluded = _excluded(ruff)
    configurations = other_configurations(root, tracked_files(root))
    baseline = _baseline(root, rule)
    on_root = [e for e in rule.exceptions if e.path == ROOT_PROJECT]
    matches = [exception.match for exception in on_root]
    excepted_files = {e.path for e in rule.exceptions if e.path != ROOT_PROJECT}
    baseline_file = rule.baseline.file if rule.baseline else "the baseline"
    violations = [
        violation(
            "an exception names no rule", "set match to the ignored rule it keeps off"
        )
        for match in matches
        if not match
    ]
    listed = set(baseline) | {match for match in matches if match}
    violations.extend(
        violation(
            f"Ruff ignores {selector}, which neither the rule's baseline nor an "
            "exception lists",
            "fix its findings and remove it from [tool.ruff.lint] ignore; only a "
            "person may add it to the baseline or make it an exception",
        )
        for selector in ignored
        if selector not in listed
    )
    violations.extend(
        violation(
            f"Ruff's extend-per-file-ignores switches rules off for {pattern}",
            "move the entry to per-file-ignores, where a registered ruff "
            "per-file-ignore rule must describe it",
        )
        for pattern in _extend_per_file_ignores(ruff)
    )
    violations.extend(
        violation(
            f"Ruff excludes {path}, which no exception lists",
            f"lint it, or have a person add an exception whose match is {path}",
        )
        for path in excluded
        if path not in matches
    )
    violations.extend(
        violation(
            f"{path} configures Ruff too, and Ruff reads it in place of the root "
            f"{ROOT_PROJECT} for the files it covers",
            "remove its Ruff settings, or have a person add an exception whose "
            f"path is {path}",
        )
        for path in configurations
        if path not in excepted_files
    )
    violations.extend(
        violation(
            f"baseline entry no longer applies: Ruff does not ignore {selector}",
            f"remove it from {baseline_file} and lower the rule's baseline count",
        )
        for selector in baseline
        if selector not in ignored
    )
    violations.extend(
        violation(
            f"the exception for {match} names a rule Ruff does not ignore",
            "remove the stale exception",
        )
        for match in matches
        if match and match not in ignored and match not in excluded
    )
    violations.extend(
        violation(
            f"the exception for {path} names no Ruff configuration",
            "remove the stale exception",
        )
        for path in sorted(excepted_files - set(configurations))
    )
    violations.extend(
        violation(
            f"{selector} is both in the baseline and an exception",
            "keep it in one of them",
        )
        for selector in sorted(set(baseline) & set(matches))
    )
    return violations


def _base_baseline(root: Path, rule: Rule, ref: str) -> list[str]:
    """Return the entries of ``rule``'s baseline file as it is on ``ref``."""
    if rule.baseline is None:
        return []
    shown = subprocess.run(
        ["git", "show", f"{ref}:{rule.baseline.file}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return baseline_lines(shown.stdout) if shown.returncode == 0 else []


def compare_with_base(
    current: Registry, base: Registry, root: Path, ref: str
) -> list[Violation]:
    """Refuse a family or an exception the base branch's rule does not list.

    A new rule, which the base branch lacks, is left to the downgrade checks.
    """
    rule, base_rule = guard_rule(current), guard_rule(base)
    if rule is None or base_rule is None:
        return []
    base_entries = set(_base_baseline(root, base_rule, ref))
    base_matches = {exception.match for exception in base_rule.exceptions}
    violations = [
        Violation(
            rule.id,
            GUARD_NAME,
            f"the baseline lists {selector}, which the base branch's does not",
            f"fix {selector}'s findings instead; a family may leave the baseline, "
            "never join it",
        )
        for selector in _baseline(root, rule)
        if selector not in base_entries
    ]
    violations.extend(
        Violation(
            rule.id,
            GUARD_NAME,
            f"the exception for {exception.match} is new: the base branch's rule "
            "has no such exception",
            f"fix {exception.match}'s findings instead; only a person may add a "
            "rule Ruff keeps off for good, by changing this check",
        )
        for exception in rule.exceptions
        if exception.match not in base_matches
    )
    return violations


def run_check(root: Path = REPO_ROOT) -> list[Violation]:
    """Load the registry under ``root`` and return every ruff-ignores violation."""
    registry, violations = load_registry(root)
    if registry is None:
        return violations
    rule = guard_rule(registry)
    return [] if rule is None else find_violations(root, rule)


def check() -> list[str]:
    """Adapter for scripts/run_quality_guards.py."""
    return [str(violation) for violation in run_check(REPO_ROOT)]
