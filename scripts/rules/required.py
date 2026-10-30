"""Check that each blocking rule runs in a job that can stop a merge.

The main branch requires one status check: the job of the pull-request
workflow (``[repository].pr_workflow``) whose name is
``[repository].required_check``. That job fails unless the jobs it needs pass,
so a rule blocks merges only when one of its ``[[rule.ci]]`` jobs is the
required job, a job it needs (directly or through other jobs' needs), or a job
of a reusable workflow in this repository that one of those jobs calls with
``uses: ./.github/workflows/...``.

A job counts, and its needs and called workflows are followed, only when it
runs on every pull request and its failure fails the run: it sets no
``continue-on-error`` other than ``false``, and has no ``if:`` unless
``[repository].pull_request_conditions`` lists the condition. The required job
itself is exempt, because it usually runs ``if: always()`` to report the
others. When a job runs the rule's check in some of its steps, at least one of
those steps must meet the same two conditions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scripts.rules.model import Violation, normalized_package
from scripts.rules.resolve import workflow_jobs
from scripts.rules.workflows import called_workflow, normalized_condition

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.rules.model import CiRef, Registry, Repository
    from scripts.rules.workflows import JobBlock, StepBlock

CHECK = "required-check"
# continue-on-error values that leave a failure failing the run.
FAILURE_FAILS_THE_RUN = ("", "false")
UNLISTED_CONDITION = "[repository].pull_request_conditions does not list"


@dataclass
class Reach:
    """The jobs the required check waits for, and the reached jobs it can't.

    Both are keyed by (workflow path, job id); ``excluded`` says why each job
    does not count.
    """

    counted: dict[tuple[str, str], JobBlock] = field(default_factory=dict)
    excluded: dict[tuple[str, str], str] = field(default_factory=dict)


def _condition_listed(condition: str, allowed: frozenset[str]) -> bool:
    """Whether a job or step with this ``if:`` runs on every pull request."""
    return not condition or normalized_condition(condition) in allowed


def _fails_the_run(continue_on_error: str) -> bool:
    """Whether a failure with this ``continue-on-error`` value fails the run."""
    return continue_on_error.strip() in FAILURE_FAILS_THE_RUN


def _job_problem(
    workflow: str, job: JobBlock, repository: Repository, allowed: frozenset[str]
) -> str | None:
    """Say why a reached job may not run or fail on a pull request, if it may."""
    where = f"job '{job.id}' in {workflow}"
    if not _fails_the_run(job.continue_on_error):
        return (
            f"{where} sets continue-on-error, so its failure does not fail "
            f"'{repository.required_check}'"
        )
    if not _condition_listed(job.condition, allowed):
        return (
            f"{where} runs only if `{normalized_condition(job.condition)}`, which "
            f"{UNLISTED_CONDITION}"
        )
    return None


def required_reach(root: Path, repository: Repository) -> Reach | None:
    """Return the jobs the required check waits for, itself included.

    Return None when the pull-request workflow has no job of that name.
    """
    workflow = normalized_package(repository.pr_workflow)
    jobs, _ = workflow_jobs(root / workflow)
    required = next(
        (job for job in jobs.values() if job.name == repository.required_check), None
    )
    if required is None:
        return None
    allowed = frozenset(repository.pull_request_conditions)
    reach = Reach()
    pending = [(workflow, required.id)]
    while pending:
        key = pending.pop()
        if key in reach.counted or key in reach.excluded:
            continue
        workflow_path, job_id = key
        job = workflow_jobs(root / workflow_path)[0].get(job_id)
        if job is None:
            continue
        problem = (
            None
            if job is required
            else _job_problem(workflow_path, job, repository, allowed)
        )
        if problem is not None:
            reach.excluded[key] = problem
            continue
        reach.counted[key] = job
        pending.extend((workflow_path, needed) for needed in job.needs)
        called = called_workflow(job.uses)
        if called is not None:
            pending.extend(
                (called, called_id) for called_id in workflow_jobs(root / called)[0]
            )
    return reach


def _step_counts(step: StepBlock, allowed: frozenset[str]) -> bool:
    """Whether a step runs on every pull request and its failure fails the job."""
    return _fails_the_run(step.continue_on_error) and _condition_listed(
        step.condition, allowed
    )


def _ref_problem(
    ref: CiRef, reach: Reach, repository: Repository, allowed: frozenset[str]
) -> str | None:
    """Say why a rule's CI job cannot stop a merge, or return None if it can."""
    key = (normalized_package(ref.workflow), ref.job)
    where = f"job '{ref.job}' in {key[0]}"
    if key in reach.excluded:
        return reach.excluded[key]
    job = reach.counted.get(key)
    if job is None:
        return f"{where} is not one that '{repository.required_check}' waits for"
    running = [step for step in job.steps if ref.invocation in step.text]
    if running and not any(_step_counts(step, allowed) for step in running):
        return (
            f"each step of {where} that runs '{ref.invocation}' sets "
            f"continue-on-error or runs only under an if: that {UNLISTED_CONDITION}"
        )
    return None


def check_required(registry: Registry, root: Path) -> list[Violation]:
    """Report each blocking rule none of whose CI jobs can stop a merge.

    Nothing is checked while the pull-request workflow is missing, which
    validation reports.
    """
    repository = registry.repository
    if not repository.pr_workflow or not (root / repository.pr_workflow).is_file():
        return []
    name = repository.required_check
    reach = required_reach(root, repository)
    if reach is None:
        return [
            Violation(
                None,
                CHECK,
                f"{repository.pr_workflow} has no job named '{name}'",
                "set [repository].required_check to the name of the job whose "
                "check the main branch requires",
            )
        ]
    allowed = frozenset(repository.pull_request_conditions)
    violations = []
    for rule in registry.rules:
        if not rule.blocking or not rule.ci:
            continue
        problems = [_ref_problem(ref, reach, repository, allowed) for ref in rule.ci]
        if all(problem is not None for problem in problems):
            violations.append(
                Violation(
                    rule.id,
                    CHECK,
                    "none of its CI jobs can stop a merge: "
                    + "; ".join(problem for problem in problems if problem),
                    f"run it in a job that '{name}' in {repository.pr_workflow} "
                    "waits for, without continue-on-error and without an if: "
                    f"that {UNLISTED_CONDITION}",
                )
            )
    return violations
