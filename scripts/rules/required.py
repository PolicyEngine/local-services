"""Check that each blocking rule runs in a job that can stop a merge.

The main branch requires one status check: the job of the pull-request
workflow (``[repository].pr_workflow``) whose name is
``[repository].required_check``. That job fails unless the jobs it needs pass,
so a rule blocks merges only when one of its ``[[rule.ci]]`` jobs is the
required job, a job it needs (directly or through other jobs' needs), or a job
of a reusable workflow in this repository that one of those jobs calls with
``uses: ./.github/workflows/...``. A rule whose jobs are all elsewhere, such
as in a workflow that runs only after merging, is reported.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.rules.model import Violation, normalized_package
from scripts.rules.resolve import workflow_jobs

if TYPE_CHECKING:
    from pathlib import Path

    from scripts.rules.model import Registry, Repository

CHECK = "required-check"
# How a job calls a reusable workflow of the same repository.
LOCAL_WORKFLOW_PREFIX = "./"


def required_jobs(root: Path, repository: Repository) -> set[tuple[str, str]] | None:
    """Return each (workflow, job id) the required check waits for, itself included.

    Return None when the pull-request workflow has no job of that name.
    """
    workflow = normalized_package(repository.pr_workflow)
    jobs, _ = workflow_jobs(root / workflow)
    required = next(
        (job for job in jobs.values() if job.name == repository.required_check), None
    )
    if required is None:
        return None
    found: set[tuple[str, str]] = set()
    pending = [(workflow, required.id)]
    while pending:
        workflow_path, job_id = pending.pop()
        if (workflow_path, job_id) in found:
            continue
        found.add((workflow_path, job_id))
        job = workflow_jobs(root / workflow_path)[0].get(job_id)
        if job is None:
            continue
        pending.extend((workflow_path, needed) for needed in job.needs)
        if job.uses.startswith(LOCAL_WORKFLOW_PREFIX):
            called = normalized_package(job.uses)
            pending.extend(
                (called, called_id) for called_id in workflow_jobs(root / called)[0]
            )
    return found


def check_required(registry: Registry, root: Path) -> list[Violation]:
    """Report each blocking rule none of whose CI jobs the required check waits for.

    Nothing is checked while the pull-request workflow is missing, which
    validation reports.
    """
    repository = registry.repository
    if not repository.pr_workflow or not (root / repository.pr_workflow).is_file():
        return []
    name = repository.required_check
    required = required_jobs(root, repository)
    if required is None:
        return [
            Violation(
                None,
                CHECK,
                f"{repository.pr_workflow} has no job named '{name}'",
                "set [repository].required_check to the name of the job whose "
                "check the main branch requires",
            )
        ]
    return [
        Violation(
            rule.id,
            CHECK,
            f"none of its CI jobs is one that '{name}' waits for, so it cannot "
            "stop a merge",
            f"add its job to the needs of the job named '{name}' in "
            f"{repository.pr_workflow}",
        )
        for rule in registry.rules
        if rule.blocking
        and rule.ci
        and not any(
            (normalized_package(ref.workflow), ref.job) in required for ref in rule.ci
        )
    ]
