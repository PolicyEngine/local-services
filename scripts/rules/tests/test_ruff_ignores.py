"""Tests for the ruff-ignores guard: Ruff ignores only what the registry lists."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.rules.ruff_ignores import run_check
from scripts.rules.tests.support import REPOSITORY_TABLE

FIXTURES = Path(__file__).parent / "fixtures" / "ruff_ignores"
BASELINE = "rules/baselines/ruff-families-not-yet-fixed.txt"
RULE = "ex.quality.ruff-families-not-yet-fixed"
REGISTRY = (
    """
schema_version = 1
repo = "example"
issue_repository = "example/repo"
"""
    + REPOSITORY_TABLE
    + f"""
[[category]]
id = "quality"
title = "Quality"

[[rule]]
id = "{RULE}"
category = "quality"
statement = "Ruff ignores only the rules this rule lists."
rationale = "pyproject.toml"
scope = ["pyproject.toml"]
tier = "mechanical"
status = "ratcheted"
autofix = false
[rule.enforcer]
kind = "quality-guard"
name = "ruff-ignores"
[[rule.ci]]
workflow = ".github/workflows/pr.yml"
job = "rules"
invocation = "run_quality_guards.py"
[rule.baseline]
file = "{BASELINE}"
count = 1
[rule.downgrade]
reason = "Not fixed yet."
issue = 54
[[rule.exception]]
path = "pyproject.toml"
match = "COM812"
reason = "Conflicts with ruff format."
"""
)


class GuardCase(unittest.TestCase):
    def messages(
        self,
        ignore: list[str],
        baseline: str = "ANN\n",
        registry: str = REGISTRY,
        extend_ignore: list[str] | None = None,
    ) -> list[str]:
        """Return the guard's messages for a tree ignoring ``ignore``."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "rules" / "baselines").mkdir(parents=True)
        (root / "rules" / "registry.toml").write_text(registry)
        (root / BASELINE).write_text("# Families not fixed yet.\n" + baseline)
        lint = f'[tool.ruff.lint]\nselect = ["ALL"]\nignore = {ignore!r}\n'.replace(
            "'", '"'
        )
        if extend_ignore is not None:
            lint += f"extend-ignore = {extend_ignore!r}\n".replace("'", '"')
        (root / "pyproject.toml").write_text(lint)
        return [str(violation) for violation in run_check(root)]


class TestRuffIgnoresProof(unittest.TestCase):
    """Proof: an ignored family the registry doesn't list fails; listed ones pass."""

    def run_on(self, fixture: str) -> list[str]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "rules" / "baselines").mkdir(parents=True)
        (root / "rules" / "registry.toml").write_text(REGISTRY)
        (root / BASELINE).write_text("ANN\n")
        shutil.copy(FIXTURES / fixture, root / "pyproject.toml")
        return [violation.message for violation in run_check(root)]

    def test_rule_proof_ruff_ignores(self) -> None:
        self.assertEqual(
            self.run_on("bad_pyproject.toml"),
            [
                "Ruff ignores S, which neither the rule's baseline nor an exception lists"
            ],
        )
        self.assertEqual(self.run_on("good_pyproject.toml"), [])


class TestIgnores(GuardCase):
    def test_ignores_the_baseline_and_the_exceptions_list_pass(self) -> None:
        self.assertEqual(self.messages(["COM812", "ANN"]), [])

    def test_an_unlisted_ignore_fails_and_says_who_may_list_it(self) -> None:
        self.assertEqual(
            self.messages(["COM812", "ANN", "D"]),
            [
                f"[{RULE}] ruff-ignores: Ruff ignores D, which neither the rule's "
                "baseline nor an exception lists. Fix: fix its findings and remove "
                "it from [tool.ruff.lint] ignore; only a person may add it to the "
                "baseline or make it an exception."
            ],
        )

    def test_extend_ignore_counts_as_ignoring(self) -> None:
        self.assertEqual(len(self.messages(["COM812", "ANN"], extend_ignore=["PT"])), 1)

    def test_a_fixed_family_still_in_the_baseline_fails_so_the_baseline_shrinks(
        self,
    ) -> None:
        self.assertEqual(
            self.messages(["COM812"]),
            [
                f"[{RULE}] ruff-ignores: baseline entry no longer applies: Ruff does "
                f"not ignore ANN. Fix: remove it from {BASELINE} and lower the "
                "rule's baseline count."
            ],
        )

    def test_an_exception_for_a_rule_ruff_does_not_ignore_is_stale(self) -> None:
        self.assertEqual(
            self.messages(["ANN"]),
            [
                f"[{RULE}] ruff-ignores: the exception for COM812 names a rule Ruff "
                "does not ignore. Fix: remove the stale exception."
            ],
        )

    def test_a_rule_in_the_baseline_and_an_exception_fails(self) -> None:
        self.assertIn(
            f"[{RULE}] ruff-ignores: COM812 is both in the baseline and an "
            "exception. Fix: keep it in one of them.",
            self.messages(["COM812", "ANN"], baseline="ANN\nCOM812\n"),
        )

    def test_an_exception_names_the_rule_it_keeps_off(self) -> None:
        registry = REGISTRY.replace('match = "COM812"\n', "")

        self.assertIn(
            f"[{RULE}] ruff-ignores: an exception names no rule. Fix: set match "
            "to the ignored rule it keeps off.",
            self.messages(["ANN"], registry=registry),
        )

    def test_without_the_rule_nothing_is_checked(self) -> None:
        registry = REGISTRY.replace('name = "ruff-ignores"', 'name = "other"')

        self.assertEqual(self.messages(["S"], registry=registry), [])


if __name__ == "__main__":
    unittest.main()
