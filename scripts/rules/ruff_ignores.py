"""The ruff-ignores quality guard: Ruff ignores only what the registry lists.

With every Ruff rule selected (``ALL``), ``[tool.ruff.lint] ignore`` and
``extend-ignore`` switch some off. Each ignored selector must be listed by
the rule this guard enforces, in one of two ways:

- in its baseline: a rule family whose findings are not fixed yet. The rule
  is ratcheted, so the guard's comparison with the base branch refuses a
  longer baseline, and this guard refuses an entry Ruff no longer ignores: the
  pull request that fixes a family removes it from both lists;
- as one of its ``[[rule.exception]]`` entries, whose ``match`` is the
  selector: a rule that stays off, such as one that conflicts with ruff
  format, with its reason.

The guard fails on an ignored selector neither lists, on a baseline entry or
an exception that names a selector Ruff does not ignore, and on a selector
listed both ways. It reads the configuration only; it does not run Ruff, so
it cannot tell whether a listed family still has findings.
"""

from __future__ import annotations

from pathlib import Path

from scripts.rules.load import load_registry
from scripts.rules.model import Kind, Registry, Rule, Violation
from scripts.rules.resolve import ruff_lint_config

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_NAME = "ruff-ignores"


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


def ignored_selectors(root: Path) -> list[str]:
    """Return what Ruff's ``ignore`` and ``extend-ignore`` list, in order."""
    lint = ruff_lint_config(root)
    return [*lint.get("ignore", []), *lint.get("extend-ignore", [])]


def _baseline(root: Path, rule: Rule) -> list[str]:
    """Return the selectors the rule's baseline lists, skipping comments."""
    if rule.baseline is None or not (root / rule.baseline.file).is_file():
        return []
    return [
        line.strip()
        for line in (root / rule.baseline.file).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def find_violations(root: Path, rule: Rule) -> list[Violation]:
    """Compare what Ruff ignores with what ``rule`` lists."""

    def violation(message: str, fix: str) -> Violation:
        return Violation(rule.id, GUARD_NAME, message, fix)

    ignored = set(ignored_selectors(root))
    baseline = _baseline(root, rule)
    exceptions = [exception.match for exception in rule.exceptions]
    baseline_file = rule.baseline.file if rule.baseline else "the baseline"
    violations = [
        violation(
            "an exception names no rule", "set match to the ignored rule it keeps off"
        )
        for match in exceptions
        if not match
    ]
    listed = set(baseline) | {match for match in exceptions if match}
    violations.extend(
        violation(
            f"Ruff ignores {selector}, which neither the rule's baseline nor an "
            "exception lists",
            "fix its findings and remove it from [tool.ruff.lint] ignore; only a "
            "person may add it to the baseline or make it an exception",
        )
        for selector in ignored_selectors(root)
        if selector not in listed
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
        for match in exceptions
        if match and match not in ignored
    )
    violations.extend(
        violation(
            f"{selector} is both in the baseline and an exception",
            "keep it in one of them",
        )
        for selector in sorted(set(baseline) & set(exceptions))
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
