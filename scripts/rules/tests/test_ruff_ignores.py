"""Tests for the ruff-ignores guard: Ruff ignores only what the registry lists."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from scripts.rules.baseline import load_base_registry
from scripts.rules.load import load_registry
from scripts.rules.ruff_ignores import compare_with_base, run_check
from scripts.rules.tests.support import REPOSITORY_TABLE
from scripts.rules.validate import validate

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
        more_configuration: str = "",
        files: dict[str, str] | None = None,
    ) -> list[str]:
        """Return the guard's messages for a tree ignoring ``ignore``."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for relative, content in (files or {}).items():
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(content)
        (root / "rules" / "baselines").mkdir(parents=True)
        (root / "rules" / "registry.toml").write_text(registry)
        (root / BASELINE).write_text("# Families not fixed yet.\n" + baseline)
        lint = f'[tool.ruff.lint]\nselect = ["ALL"]\nignore = {ignore!r}\n'.replace(
            "'", '"'
        )
        if extend_ignore is not None:
            lint += f"extend-ignore = {extend_ignore!r}\n".replace("'", '"')
        (root / "pyproject.toml").write_text(more_configuration + lint)
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
        self.assertEqual(
            self.messages(["COM812", "ANN"], extend_ignore=["PT"]),
            [
                f"[{RULE}] ruff-ignores: Ruff ignores PT, which neither the rule's "
                "baseline nor an exception lists. Fix: fix its findings and remove "
                "it from [tool.ruff.lint] ignore; only a person may add it to the "
                "baseline or make it an exception."
            ],
        )

    def test_a_baseline_family_ruff_no_longer_ignores_fails_so_the_baseline_shrinks(
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

    def test_the_top_level_ignore_lists_count_as_ignoring(self) -> None:
        for key in ("ignore", "extend-ignore"):
            with self.subTest(key=key):
                self.assertIn(
                    f"[{RULE}] ruff-ignores: Ruff ignores S, which neither the "
                    "rule's baseline nor an exception lists. Fix: fix its findings "
                    "and remove it from [tool.ruff.lint] ignore; only a person may "
                    "add it to the baseline or make it an exception.",
                    self.messages(
                        ["COM812", "ANN"],
                        more_configuration=f'[tool.ruff]\n{key} = ["S"]\n\n',
                    ),
                )

    def test_extend_per_file_ignores_are_refused(self) -> None:
        self.assertEqual(
            self.messages(
                ["COM812", "ANN"],
                more_configuration=(
                    '[tool.ruff.lint.extend-per-file-ignores]\n"src/*.py" = ["S"]\n\n'
                ),
            ),
            [
                f"[{RULE}] ruff-ignores: Ruff's extend-per-file-ignores switches "
                "rules off for src/*.py. Fix: move the entry to per-file-ignores, "
                "where a registered ruff per-file-ignore rule must describe it."
            ],
        )

    def test_an_excluded_path_needs_an_exception(self) -> None:
        for key in ("exclude", "extend-exclude"):
            with self.subTest(key=key):
                configuration = f'[tool.ruff]\n{key} = ["src/legacy"]\n\n'
                self.assertEqual(
                    self.messages(["COM812", "ANN"], more_configuration=configuration),
                    [
                        f"[{RULE}] ruff-ignores: Ruff excludes src/legacy, which no "
                        "exception lists. Fix: lint it, or have a person add an "
                        "exception whose match is src/legacy."
                    ],
                )
                excepted = REGISTRY + (
                    "\n[[rule.exception]]\n"
                    'path = "pyproject.toml"\n'
                    'match = "src/legacy"\n'
                    'reason = "Generated code."\n'
                )
                self.assertEqual(
                    self.messages(
                        ["COM812", "ANN"],
                        registry=excepted,
                        more_configuration=configuration,
                    ),
                    [],
                )

    def test_another_ruff_configuration_needs_an_exception(self) -> None:
        for path, content in (
            ("ruff.toml", "[lint]\nignore = []\n"),
            (".ruff.toml", "[lint]\nignore = []\n"),
            ("packages/one/ruff.toml", "[lint]\nignore = []\n"),
            ("packages/one/pyproject.toml", "[tool.ruff.lint]\nignore = []\n"),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.messages(["COM812", "ANN"], files={path: content}),
                    [
                        f"[{RULE}] ruff-ignores: {path} configures Ruff too, and "
                        "Ruff reads it in place of the root pyproject.toml for the "
                        "files it covers. Fix: remove its Ruff settings, or have a "
                        f"person add an exception whose path is {path}."
                    ],
                )

    def test_a_package_without_ruff_settings_is_no_configuration(self) -> None:
        self.assertEqual(
            self.messages(
                ["COM812", "ANN"],
                files={"packages/one/pyproject.toml": '[project]\nname = "one"\n'},
            ),
            [],
        )

    def test_without_the_rule_nothing_is_checked(self) -> None:
        registry = REGISTRY.replace('name = "ruff-ignores"', 'name = "other"')

        self.assertEqual(self.messages(["S"], registry=registry), [])


class TestBaseComparison(unittest.TestCase):
    """What Ruff ignores may only shrink: no new family, and no new exception."""

    def compare(self, baseline: str, ignore: list[str], registry: str = REGISTRY):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]

        def write(baseline_text: str, ignored: list[str], registry_text: str) -> None:
            (root / "rules" / "baselines").mkdir(parents=True, exist_ok=True)
            count = len(baseline_text.split())
            (root / "rules" / "registry.toml").write_text(
                registry_text.replace("count = 1", f"count = {count}")
            )
            (root / BASELINE).write_text(baseline_text)
            (root / "pyproject.toml").write_text(
                f"[tool.ruff.lint]\nignore = {ignored!r}\n".replace("'", '"')
            )

        write("ANN\nPT\n", ["COM812", "ANN", "PT"], REGISTRY)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run([*git, "add", "-A"], cwd=root, check=True)
        subprocess.run([*git, "commit", "-qm", "base"], cwd=root, check=True)
        write(baseline, ignore, registry)
        current, _ = load_registry(root)
        base = load_base_registry(root, "HEAD")
        assert current is not None and base is not None
        return [
            str(violation)
            for violation in compare_with_base(current, base, root, "HEAD")
        ]

    def test_removing_a_family_passes(self) -> None:
        self.assertEqual(self.compare("ANN\n", ["COM812", "ANN"]), [])

    def test_swapping_a_fixed_family_for_another_fails(self) -> None:
        self.assertEqual(
            self.compare("ANN\nF\n", ["COM812", "ANN", "F"]),
            [
                f"[{RULE}] ruff-ignores: the baseline lists F, which the base "
                "branch's does not. Fix: fix F's findings instead; a family may "
                "leave the baseline, never join it."
            ],
        )

    def test_moving_a_family_into_a_new_exception_fails(self) -> None:
        registry = REGISTRY + (
            "\n[[rule.exception]]\n"
            'path = "pyproject.toml"\n'
            'match = "PT"\n'
            'reason = "Kept off."\n'
        )

        self.assertEqual(
            self.compare("ANN\n", ["COM812", "ANN", "PT"], registry),
            [
                f"[{RULE}] ruff-ignores: the exception for PT is new: the base "
                "branch's rule has no such exception. Fix: fix PT's findings "
                "instead; only a person may add a rule Ruff keeps off for good, "
                "by changing this check."
            ],
        )


class TestDowngradeStays(unittest.TestCase):
    """A ratcheted rule with tolerated entries keeps its [rule.downgrade]."""

    def test_a_ratcheted_rule_with_entries_needs_its_downgrade(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "rules" / "baselines").mkdir(parents=True)
        (root / BASELINE).write_text("ANN\n")
        (root / "pyproject.toml").write_text("[tool.ruff.lint]\n")
        without = REGISTRY.replace(
            '[rule.downgrade]\nreason = "Not fixed yet."\nissue = 54\n', ""
        )
        (root / "rules" / "registry.toml").write_text(without)
        registry, _ = load_registry(root)
        assert registry is not None

        self.assertIn(
            f"[{RULE}] schema: a ratcheted rule that tolerates 1 entries has no "
            "[rule.downgrade]. Fix: only a person may weaken a rule, by adding "
            '[rule.downgrade] with reason = "..." and issue = <number of the open '
            "issue that tracks restoring the rule>.",
            [
                str(violation)
                for violation in validate(registry, root, date(2026, 10, 30))
            ],
        )


if __name__ == "__main__":
    unittest.main()
