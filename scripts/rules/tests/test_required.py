"""Tests for the check that each blocking rule runs where it can stop a merge.

The main branch requires one status check: the pull-request workflow's job
named ``[repository].required_check``. A blocking rule must run in that job,
in a job it needs (directly or through other jobs), or in a job of a reusable
workflow of this repository that one of those jobs calls. A job, or the step
that runs the rule's check, counts only when it runs on every pull request and
its failure fails the run: no ``continue-on-error``, and no ``if:`` unless
``[repository].pull_request_conditions`` lists the condition.
"""

from __future__ import annotations

import unittest

from scripts.rules.tests.support import WORKFLOW, Repo

PR_WORKFLOW = ".github/workflows/pr.yml"
ALL_NEEDS = "needs: [test, lint, typecheck, quality-guards]"
CAN_STOP_A_MERGE_FIX = (
    "Fix: run it in a job that 'CI complete' in .github/workflows/pr.yml "
    "waits for, without continue-on-error and without an if: that "
    "[repository].pull_request_conditions does not list."
)
NOT_WAITED_FOR = "job '{job}' in {workflow} is not one that 'CI complete' waits for"


def types_cannot_stop_a_merge(*problems: str) -> str:
    """Return the violation for the type-check rule, with each job's problem."""
    return (
        "[ex.quality.types] required-check: none of its CI jobs can stop a merge: "
        + "; ".join(problems)
        + ". "
        + CAN_STOP_A_MERGE_FIX
    )


TYPES_NOT_REQUIRED = types_cannot_stop_a_merge(
    NOT_WAITED_FOR.format(job="typecheck", workflow=".github/workflows/pr.yml")
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
            self.assertEqual(
                required_check_messages(repo),
                [
                    types_cannot_stop_a_merge(
                        NOT_WAITED_FOR.format(
                            job="mypy", workflow=".github/workflows/typecheck.yml"
                        )
                    )
                ],
            )

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
            self.assertEqual(
                required_check_messages(repo),
                [
                    types_cannot_stop_a_merge(
                        NOT_WAITED_FOR.format(
                            job="typecheck", workflow=".github/workflows/pr.yml"
                        ),
                        NOT_WAITED_FOR.format(
                            job="typecheck", workflow=".github/workflows/push.yml"
                        ),
                    )
                ],
            )

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


def called_typecheck(
    repo: Repo, job_settings: str = "", step_settings: str = ""
) -> None:
    """Run the type check in a reusable workflow that ``verify`` calls.

    ``job_settings`` and ``step_settings`` are added to its job and its step.
    """
    repo.write(
        ".github/workflows/typecheck.yml",
        "on: workflow_call\njobs:\n  mypy:\n    runs-on: ubuntu-latest\n"
        + job_settings
        + "    steps:\n      - run: uv run mypy\n"
        + step_settings,
    )
    repo.edit(
        "rules/registry.toml",
        'workflow = ".github/workflows/pr.yml"\njob = "typecheck"',
        'workflow = ".github/workflows/typecheck.yml"\njob = "mypy"',
    )
    repo.edit(PR_WORKFLOW, ALL_NEEDS, "needs: [test, lint, verify, quality-guards]")
    repo.write(PR_WORKFLOW, repo.read(PR_WORKFLOW) + CALL_TYPECHECK)


MYPY = "job 'mypy' in .github/workflows/typecheck.yml"


class ConditionsTest(unittest.TestCase):
    """Jobs and steps that may not run, or may fail, on a pull request."""

    def test_a_job_that_is_skipped_on_pull_requests_cannot_stop_a_merge(
        self,
    ) -> None:
        with Repo() as repo:
            called_typecheck(repo, "    if: github.event_name == 'push'\n")

            self.assertEqual(
                required_check_messages(repo),
                [
                    types_cannot_stop_a_merge(
                        f"{MYPY} runs only if `github.event_name == 'push'`, which "
                        "[repository].pull_request_conditions does not list"
                    )
                ],
            )

    def test_a_job_that_may_fail_cannot_stop_a_merge(self) -> None:
        for value in ("true", "${{ matrix.experimental }}"):
            with self.subTest(value=value), Repo() as repo:
                called_typecheck(repo, f"    continue-on-error: {value}\n")

                self.assertEqual(
                    required_check_messages(repo),
                    [
                        types_cannot_stop_a_merge(
                            f"{MYPY} sets continue-on-error, so its failure does "
                            "not fail 'CI complete'"
                        )
                    ],
                )

    def test_continue_on_error_false_still_counts(self) -> None:
        with Repo() as repo:
            called_typecheck(repo, "    continue-on-error: false\n")

            self.assertEqual(required_check_messages(repo), [])

    def test_a_listed_condition_counts_with_or_without_braces(self) -> None:
        with Repo() as repo:
            called_typecheck(
                repo, "    if: ${{ github.event_name == 'pull_request' }}\n"
            )
            repo.edit(
                "rules/registry.toml",
                'required_check = "CI complete"',
                'required_check = "CI complete"\n'
                "pull_request_conditions = [\"github.event_name == 'pull_request'\"]",
            )

            self.assertEqual(required_check_messages(repo), [])

    def test_a_step_that_is_skipped_or_may_fail_cannot_stop_a_merge(self) -> None:
        for setting in (
            "        continue-on-error: true\n",
            "        if: github.event_name == 'push'\n",
        ):
            with self.subTest(setting=setting.strip()), Repo() as repo:
                called_typecheck(repo, step_settings=setting)

                self.assertEqual(
                    required_check_messages(repo),
                    [
                        types_cannot_stop_a_merge(
                            f"each step of {MYPY} that runs 'mypy' sets "
                            "continue-on-error or runs only under an if: that "
                            "[repository].pull_request_conditions does not list"
                        )
                    ],
                )

    def test_needs_are_not_followed_through_a_job_with_a_condition(self) -> None:
        # A job that runs always() also runs when the jobs it needs failed.
        with Repo() as repo:
            repo.edit(PR_WORKFLOW, ALL_NEEDS, "needs: gate")
            repo.write(
                PR_WORKFLOW,
                repo.read(PR_WORKFLOW)
                + "  gate:\n    if: always()\n"
                + "    needs: [test, lint, typecheck, quality-guards]\n"
                + "    steps:\n      - run: echo gate\n",
            )

            messages = required_check_messages(repo)

        self.assertEqual(len(messages), 5, messages)
        self.assertIn(TYPES_NOT_REQUIRED, messages)

    def test_the_required_job_itself_may_run_always(self) -> None:
        with Repo() as repo:
            self.assertIn("    if: always()\n", repo.read(PR_WORKFLOW))
            self.assertEqual(required_check_messages(repo), [])

    def test_a_matrix_job_counts_by_its_id(self) -> None:
        with Repo() as repo:
            repo.edit(
                PR_WORKFLOW,
                "  typecheck:\n    runs-on: ubuntu-latest\n",
                "  typecheck:\n    runs-on: ubuntu-latest\n"
                "    strategy:\n      matrix:\n        python: ['3.12', '3.13']\n",
            )

            self.assertEqual(required_check_messages(repo), [])

    def test_a_workflow_path_starting_with_dot_slash_matches(self) -> None:
        with Repo() as repo:
            repo.edit(
                "rules/registry.toml",
                'workflow = ".github/workflows/pr.yml"\njob = "typecheck"',
                'workflow = "./.github/workflows/pr.yml"\njob = "typecheck"',
            )

            self.assertEqual(required_check_messages(repo), [])


if __name__ == "__main__":
    unittest.main()
