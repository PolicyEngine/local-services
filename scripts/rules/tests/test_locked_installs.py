"""Tests for the locked-installs guard: lock, install and test from uv.lock."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.rules.load import load_text
from scripts.rules.locked_installs import (
    REPO_ROOT,
    find_violations,
    lock_violations,
    relock_violations,
    scan,
    tracked_files,
)
from scripts.rules.model import Repository
from scripts.rules.tests.support import REPOSITORY_TABLE

FIXTURES = Path(__file__).parent / "fixtures" / "locked_installs"
# The example repository's rule ids, from its rule_prefix "ex".
INSTALL_RULE = "ex.dependencies.install-from-lock"
LOCK_RULE = "ex.dependencies.lock-committed"
# The layout of the proof fixtures: scripts under .github/scripts.
FIXTURE_REPOSITORY = Repository(
    rule_prefix="ex",
    pr_workflow=".github/workflows/pr.yml",
    script_directories=(".github/scripts",),
)
REGISTRY_HEADER = (
    """
schema_version = 1
repo = "example"
"""
    + REPOSITORY_TABLE
    + """

[[category]]
id = "dependencies"
title = "Dependencies"
"""
)
RULE_TEMPLATE = """
[[rule]]
id = "{rule_id}"
category = "dependencies"
statement = "Installs come from uv.lock."
rationale = "docs/x.md"
scope = ["**"]
tier = "mechanical"
status = "enforced"
autofix = false
[rule.enforcer]
kind = "quality-guard"
name = "locked-installs"
"""


def registry_with(exceptions: str = "", rule_id: str = INSTALL_RULE) -> object:
    text = REGISTRY_HEADER + RULE_TEMPLATE.format(rule_id=rule_id) + exceptions
    registry, violations = load_text(text)
    assert violations == [], violations
    return registry


def violation_messages(root: Path, exceptions: str = "") -> list[str]:
    return [
        violation.message
        for violation in find_violations(root, registry_with(exceptions))
        if violation.rule_id == INSTALL_RULE
    ]


class TestLockedInstallsProof(unittest.TestCase):
    def test_rule_proof_install_from_lock(self) -> None:
        bad = scan(FIXTURES / "install_bad")
        good = scan(FIXTURES / "install_good")

        self.assertEqual(
            sorted(site.text for site in bad.unlocked_installs),
            [
                "pip install policyengine",
                "pip install requests",
                "pip_install(policyengine-us)",
                "uv sync --extra test",
            ],
        )
        self.assertEqual(good.unlocked_installs, [])

    def test_rule_proof_test_from_lock(self) -> None:
        bad = scan(FIXTURES / "test_bad")
        good = scan(FIXTURES / "test_good")

        self.assertEqual(
            sorted(site.text for site in bad.unlocked_tests),
            ["coverage run -m pytest", "python -m pytest tests/"],
        )
        self.assertEqual(good.unlocked_tests, [])

    def test_rule_proof_lock_committed(self) -> None:
        bad_root = FIXTURES / "lock_bad"
        good_root = FIXTURES / "lock_good"

        self.assertEqual(
            [
                violation.message
                for violation in lock_violations(
                    bad_root, tracked_files(bad_root), FIXTURE_REPOSITORY
                )
            ],
            [
                "app/pyproject.toml has no committed uv.lock",
                "no .github/workflows/pr.yml job runs uv lock --check",
            ],
        )
        self.assertEqual(
            lock_violations(good_root, tracked_files(good_root), FIXTURE_REPOSITORY),
            [],
        )

    def test_rule_proof_scheduled_relock(self) -> None:
        bad_root = FIXTURES / "relock_bad"
        good_root = FIXTURES / "relock_good"

        self.assertEqual(
            [
                violation.message
                for violation in relock_violations(
                    bad_root, tracked_files(bad_root), FIXTURE_REPOSITORY
                )
            ],
            [".github/workflows/update.yml does not relock projects/two/uv.lock"],
        )
        self.assertEqual(
            relock_violations(good_root, tracked_files(good_root), FIXTURE_REPOSITORY),
            [],
        )


class TestCommandClassification(unittest.TestCase):
    def scan_script(self, script: str) -> tuple[list[str], list[str]]:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "run.sh").write_text(script, encoding="utf-8")
        findings = scan(root)
        return (
            [site.text for site in findings.unlocked_installs],
            [site.text for site in findings.unlocked_tests],
        )

    def test_frozen_and_locked_syncs_and_runs_are_locked(self) -> None:
        installs, tests = self.scan_script(
            "uv sync --frozen --extra test\n"
            "uv sync --locked\n"
            "uv run --frozen pytest tests/\n"
            "uv run --locked python -m unittest\n"
            "uv lock --upgrade\n"
        )

        self.assertEqual((installs, tests), ([], []))

    def test_every_unlocked_installer_is_reported(self) -> None:
        installs, _tests = self.scan_script(
            "uv run python -m tool\n"
            "uv run --frozen --with requests python x.py\n"
            "uv pip install requests\n"
            "uv tool install ruff\n"
            "uvx ruff check\n"
            "python -m pip install requests\n"
            "pipx install poetry\n"
            "uv add requests\n"
            "poetry install\n"
        )

        self.assertEqual(len(installs), 9, installs)

    def test_continuations_and_chains_are_read_as_commands(self) -> None:
        installs, tests = self.scan_script(
            "cd app && uv sync \\\n  --extra test && uv run pytest\n"
        )

        self.assertEqual(installs, ["uv sync  --extra test", "uv run pytest"])
        self.assertEqual(tests, ["uv run pytest"])

    def test_text_tools_and_shell_keywords_do_not_hide_or_invent_installs(
        self,
    ) -> None:
        installs, _tests = self.scan_script(
            "VERSION := $(shell sed -n 's/.*uv tool install ruff==\\(.*\\)/\\1/p' a)\n"
            'if [ -z "$V" ]; then echo "uv tool install ruff==<version>"; fi\n'
            'name="$(uv run python -c pass)"\n'
            "RUFF := uv tool run ruff@$(VERSION)\n"
        )

        self.assertEqual(
            installs,
            ["name=$(uv run python -c pass)", "RUFF := uv tool run ruff@$(VERSION)"],
        )

    def test_comments_and_echoed_instructions_are_not_commands(self) -> None:
        installs, tests = self.scan_script(
            '# uv sync --extra test\necho "  uv sync"\nprintf "pip install x"\n'
        )

        self.assertEqual((installs, tests), ([], []))

    def test_workflow_sites_are_reported_on_their_own_line(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        workflow = root / ".github" / "workflows" / "ci.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text(
            "name: CI\n"
            "on: pull_request\n"
            "jobs:\n"
            "  first:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: uv run python one.py\n"
            "  second:\n"
            "    runs-on: ubuntu-latest\n"
            "    steps:\n"
            "      - run: |\n"
            "          echo start\n"
            "          uv run python two.py\n",
            encoding="utf-8",
        )

        findings = scan(root)

        self.assertEqual(
            [str(site) for site in findings.unlocked_installs],
            [
                ".github/workflows/ci.yml:7: uv run python one.py",
                ".github/workflows/ci.yml:13: uv run python two.py",
            ],
        )


class TestExceptions(unittest.TestCase):
    def test_an_exception_allows_only_the_site_it_names(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
match = "pip install requests"
reason = "example"
removal = "never"
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertFalse(any("pip install requests" in m for m in messages))
        self.assertTrue(any("uv sync --extra test" in m for m in messages))

    def test_an_exception_needs_a_site_and_an_end(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
reason = "no site and no end"
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertIn("exception for .github/workflows/ci.yml names no site", messages)
        self.assertIn(
            "exception for .github/workflows/ci.yml has no removal condition "
            "and no review date with a tracking issue",
            messages,
        )

    def test_a_dated_exception_with_an_issue_is_accepted(self) -> None:
        exceptions = """
[[rule.exception]]
path = "Dockerfile"
match = "pip install policyengine"
reason = "example"
expires = "2099-01-01"
issue = 1
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertFalse(any("Dockerfile" in m for m in messages), messages)

    def test_a_pinned_tool_exception_must_pin_an_exact_version(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
match = "pip install requests"
category = "pinned-tool"
reason = "example"
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertIn(
            "pinned-tool exception for .github/workflows/ci.yml does not pin an "
            "exact version",
            messages,
        )

    def test_a_tool_pinned_with_an_at_version_is_a_pinned_tool(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "Makefile").write_text("RUFF := uv tool run ruff@$(VERSION)\n")
        exceptions = """
[[rule.exception]]
path = "Makefile"
match = "uv tool run ruff@$(VERSION)"
category = "pinned-tool"
reason = "example"
"""
        self.assertEqual(violation_messages(root, exceptions), [])

    def test_a_tool_at_a_moving_version_is_not_a_pinned_tool(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "Makefile").write_text("RUFF := uv tool run ruff@latest\n")
        exceptions = """
[[rule.exception]]
path = "Makefile"
match = "uv tool run ruff@latest"
category = "pinned-tool"
reason = "example"
"""
        self.assertEqual(
            violation_messages(root, exceptions),
            ["pinned-tool exception for Makefile does not pin an exact version"],
        )

    def test_an_issue_without_a_review_date_is_not_an_end(self) -> None:
        exceptions = """
[[rule.exception]]
path = "Dockerfile"
match = "pip install policyengine"
reason = "example"
issue = 1
"""
        messages = violation_messages(FIXTURES / "install_bad", exceptions)

        self.assertIn(
            "exception for Dockerfile has no removal condition and no review "
            "date with a tracking issue",
            messages,
        )

    def test_an_exception_that_matches_no_site_is_stale(self) -> None:
        exceptions = """
[[rule.exception]]
path = ".github/workflows/ci.yml"
match = "pip install gone"
reason = "example"
removal = "never"
"""
        messages = violation_messages(FIXTURES / "install_good", exceptions)

        self.assertEqual(
            messages,
            [
                "exception for .github/workflows/ci.yml (pip install gone) "
                "matches no site"
            ],
        )


class TestRepositoryLayout(unittest.TestCase):
    """The checks read rule ids, the PR workflow and script directories from
    the registry's [repository] table."""

    def lock_messages(self, files: dict[str, str], repository: Repository) -> list:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text, encoding="utf-8")
        return [
            str(violation)
            for violation in lock_violations(root, tracked_files(root), repository)
        ]

    def test_rule_ids_start_with_the_registry_prefix(self) -> None:
        registry = load_text(
            (REGISTRY_HEADER + RULE_TEMPLATE.format(rule_id=INSTALL_RULE))
            .replace('rule_prefix = "ex"', 'rule_prefix = "emitter"')
            .replace('id = "ex.', 'id = "emitter.')
        )[0]

        violations = find_violations(FIXTURES / "lock_bad", registry)

        self.assertEqual(
            sorted({violation.rule_id for violation in violations}),
            [
                "emitter.dependencies.lock-committed",
                "emitter.dependencies.scheduled-relock",
            ],
        )

    def test_the_lock_check_must_run_in_the_named_pr_workflow(self) -> None:
        files = {
            ".github/workflows/pr.yaml": (
                "on: pull_request\njobs:\n  locks:\n    steps:\n"
                "      - run: uv lock --check\n"
            ),
        }
        named = Repository(rule_prefix="ex", pr_workflow=".github/workflows/pr.yaml")
        other = Repository(rule_prefix="ex", pr_workflow=".github/workflows/pr.yml")

        self.assertEqual(self.lock_messages(files, named), [])
        self.assertEqual(
            self.lock_messages(files, other),
            [
                f"[{LOCK_RULE}] locked-installs: no .github/workflows/pr.yml job "
                "runs uv lock --check. Fix: run uv lock --check in a job of "
                ".github/workflows/pr.yml."
            ],
        )

    def test_workflow_commands_are_followed_into_the_script_directories(
        self,
    ) -> None:
        files = {
            ".github/workflows/pr.yaml": (
                "on: pull_request\njobs:\n  locks:\n    steps:\n"
                "      - run: bash tools/check-locks.sh\n"
            ),
            "tools/check-locks.sh": "uv lock --check\n",
        }
        tools = Repository(
            rule_prefix="ex",
            pr_workflow=".github/workflows/pr.yaml",
            script_directories=("tools",),
        )
        elsewhere = Repository(
            rule_prefix="ex",
            pr_workflow=".github/workflows/pr.yaml",
            script_directories=("scripts",),
        )

        self.assertEqual(self.lock_messages(files, tools), [])
        self.assertEqual(len(self.lock_messages(files, elsewhere)), 1)

    def test_a_script_is_no_lock_check_by_its_name(self) -> None:
        files = {
            ".github/workflows/pr.yml": (
                "on: pull_request\njobs:\n  locks:\n    steps:\n"
                "      - run: .github/scripts/check-uv-locks.sh\n"
            ),
            ".github/scripts/check-uv-locks.sh": "echo checked\n",
        }

        self.assertEqual(len(self.lock_messages(files, FIXTURE_REPOSITORY)), 1)


class TestRepository(unittest.TestCase):
    def test_every_install_capable_file_is_scanned(self) -> None:
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        expected = set()
        for path in listed:
            name = Path(path).name.lower()
            if "fixtures" in Path(path).parts:
                continue
            text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
            if (
                path.startswith(".github/workflows/")
                or name.endswith((".sh", ".mk"))
                or name == "makefile"
                or ("dockerfile" in name and not name.endswith(".dockerignore"))
                or "compose" in name
                or (
                    name.endswith(".py")
                    and "tests" not in Path(path).parts
                    and "modal.Image" in text
                )
            ):
                expected.add(path)

        scanned = scan(REPO_ROOT).scanned

        self.assertEqual(expected - scanned, set())


if __name__ == "__main__":
    unittest.main()
