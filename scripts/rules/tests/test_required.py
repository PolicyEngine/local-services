"""Tests for the check that each blocking rule runs where it can stop a merge.

The main branch requires one status check: the pull-request workflow's job
named ``[repository].required_check``. A blocking rule must run in that job,
in a job it needs (directly or through other jobs), or in a job of a reusable
workflow of this repository that one of those jobs calls.
"""

from __future__ import annotations

import unittest

from scripts.rules.tests.support import WORKFLOW, Repo

PR_WORKFLOW = ".github/workflows/pr.yml"
ALL_NEEDS = "needs: [test, lint, typecheck, quality-guards]"
TYPES_NOT_REQUIRED = (
    "[ex.quality.types] required-check: none of its CI jobs is one that "
    "'CI complete' waits for, so it cannot stop a merge. Fix: add its job to "
    "the needs of the job named 'CI complete' in .github/workflows/pr.yml."
)
# A reusable workflow the pull-request workflow can call for the type check.
REUSABLE_TYPECHECK = """
name: Type check
on: workflow_call
jobs:
  mypy:
    runs-on: ubuntu-latest
    steps:
      - run: uv run mypy
"""
CALL_TYPECHECK = """  verify:
    uses: ./.github/workflows/typecheck.yml
"""


def required_check_messages(repo: Repo) -> list[str]:
    """Return the guard's required-check violations for ``repo``."""
    return [message for message in repo.messages() if "required-check:" in message]


class RequiredCheckTest(unittest.TestCase):
    def test_every_blocking_rule_of_the_example_can_stop_a_merge(self) -> None:
        with Repo() as repo:
            self.assertEqual(repo.messages(), [])

    def test_a_job_the_required_check_does_not_need_is_reported(self) -> None:
        with Repo() as repo:
            repo.edit(PR_WORKFLOW, ALL_NEEDS, "needs: [test, lint, quality-guards]")

            self.assertEqual(required_check_messages(repo), [TYPES_NOT_REQUIRED])

    def test_needs_are_followed_through_other_jobs(self) -> None:
        with Repo() as repo:
            repo.edit(PR_WORKFLOW, ALL_NEEDS, "needs: gate")
            repo.write(
                PR_WORKFLOW,
                repo.read(PR_WORKFLOW)
                + "  gate:\n"
                + "    needs:\n"
                + "".join(
                    f"      - {job}\n"
                    for job in ("test", "lint", "typecheck", "quality-guards")
                )
                + "    steps:\n      - run: echo gate\n",
            )

            self.assertEqual(required_check_messages(repo), [])

    def test_jobs_of_a_called_reusable_workflow_are_required(self) -> None:
        with Repo() as repo:
            repo.write(".github/workflows/typecheck.yml", REUSABLE_TYPECHECK)
            repo.edit(
                "rules/registry.toml",
                'workflow = ".github/workflows/pr.yml"\njob = "typecheck"',
                'workflow = ".github/workflows/typecheck.yml"\njob = "mypy"',
            )
            repo.edit(
                PR_WORKFLOW, ALL_NEEDS, "needs: [test, lint, verify, quality-guards]"
            )
            repo.write(PR_WORKFLOW, repo.read(PR_WORKFLOW) + CALL_TYPECHECK)
            self.assertEqual(required_check_messages(repo), [])

            repo.edit(
                PR_WORKFLOW,
                "needs: [test, lint, verify, quality-guards]",
                "needs: [test, lint, quality-guards]",
            )
            self.assertEqual(required_check_messages(repo), [TYPES_NOT_REQUIRED])

    def test_one_required_job_is_enough(self) -> None:
        with Repo() as repo:
            repo.write(".github/workflows/push.yml", WORKFLOW)
            repo.edit(
                "rules/registry.toml",
                'job = "typecheck"\ninvocation = "mypy"\n',
                'job = "typecheck"\ninvocation = "mypy"\n'
                '[[rule.ci]]\nworkflow = ".github/workflows/push.yml"\n'
                'job = "typecheck"\ninvocation = "mypy"\n',
            )
            self.assertEqual(required_check_messages(repo), [])

            repo.edit(PR_WORKFLOW, ALL_NEEDS, "needs: [test, lint, quality-guards]")
            self.assertEqual(required_check_messages(repo), [TYPES_NOT_REQUIRED])

    def test_a_rule_that_cannot_block_is_not_checked(self) -> None:
        with Repo() as repo:
            repo.edit(PR_WORKFLOW, ALL_NEEDS, "needs: [test, lint, quality-guards]")
            repo.edit(
                "rules/registry.toml",
                'statement = "The package type-checks."\nrationale = "docs/guide.md"\n'
                'scope = ["pkg/*.py"]\ntier = "mechanical"\nstatus = "enforced"',
                'statement = "The package type-checks."\nrationale = "docs/guide.md"\n'
                'scope = ["pkg/*.py"]\ntier = "mechanical"\nstatus = "advisory"',
            )
            repo.render()

            self.assertEqual(required_check_messages(repo), [])

    def test_a_required_check_the_workflow_lacks_is_reported(self) -> None:
        with Repo() as repo:
            repo.edit(
                "rules/registry.toml",
                'required_check = "CI complete"',
                'required_check = "Required checks"',
            )

            self.assertEqual(
                required_check_messages(repo),
                [
                    "[registry] required-check: .github/workflows/pr.yml has no "
                    "job named 'Required checks'. Fix: set "
                    "[repository].required_check to the name of the job whose "
                    "check the main branch requires.",
                ],
            )


if __name__ == "__main__":
    unittest.main()
