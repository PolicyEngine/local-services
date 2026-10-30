"""Look up whether a number names an open issue in a GitHub repository.

The downgrade check (``tracking_issues.py``) uses this to confirm that each
``[rule.downgrade]`` names an open issue. It runs ``gh api``, which reads its
token from ``GH_TOKEN`` or ``GITHUB_TOKEN``. The errors say only what is wrong;
each caller adds its own fix.

A PR description check can import ``require_open_issue`` from here, so the
repository has one lookup.
"""

from __future__ import annotations

import json
import re
import subprocess
from urllib.parse import urlsplit

# gh api's error for a number GitHub has no issue or pull request for (404),
# or one whose issue was deleted (410).
MISSING_ISSUE = re.compile(r"\(HTTP (?:404|410)\)")
# The path of an issue's repository_url: [api/v3/]repos/<owner>/<name>.
REPOSITORY_PATH = re.compile(r"(?:.+/)?repos/([^/]+/[^/]+)")


class IssueLookupError(Exception):
    """GitHub couldn't be asked about an issue, or its answer wasn't an issue.

    Running the lookup again may succeed. The message says what went wrong,
    without a fix.
    """


class NotAnOpenIssueError(Exception):
    """A number names no open issue in the repository.

    ``state`` is the issue's state, such as ``closed``, when the number names an
    issue in the repository that isn't open, and None when it names no issue
    there. The message says what the number names, without a fix.
    """

    def __init__(self, message: str, *, state: str | None = None) -> None:
        """Keep ``message`` as the error's text, and the issue's ``state``."""
        super().__init__(message)
        self.state = state


def fetch_issue(repository: str, number: int) -> dict[str, object]:
    """Return GitHub's answer for issue ``number`` in ``repository``, from gh api.

    The issues API also answers for pull requests, and gh follows the redirect
    GitHub sends for an issue transferred to another repository, so the answer
    still has to be checked with ``check_issue``. Raise ``NotAnOpenIssueError``
    when GitHub has no issue ``number``, and ``IssueLookupError`` when the
    lookup fails.
    """
    completed = subprocess.run(
        ["gh", "api", f"repos/{repository}/issues/{number}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        if MISSING_ISSUE.search(completed.stderr):
            msg = f"There is no issue #{number} in {repository}."
            raise NotAnOpenIssueError(msg)
        reported = completed.stderr.strip() or f"exit status {completed.returncode}"
        msg = f"Could not look up #{number} in {repository}: {reported}."
        raise IssueLookupError(msg)
    try:
        issue = json.loads(completed.stdout)
    except json.JSONDecodeError:
        issue = None
    if not isinstance(issue, dict):
        msg = f"gh api did not print an issue for #{number}."
        raise IssueLookupError(msg)
    return issue


def _repository_of(repository_url: object) -> str | None:
    """Return ``owner/name`` from an issue's ``repository_url``, if it has one."""
    if not isinstance(repository_url, str):
        return None
    match = REPOSITORY_PATH.fullmatch(urlsplit(repository_url).path.strip("/"))
    return match.group(1) if match else None


def check_issue(repository: str, number: int, issue: dict[str, object]) -> None:
    """Return quietly if ``issue``, GitHub's answer for ``number``, is open here.

    Raise ``NotAnOpenIssueError`` when it is a pull request, an issue moved to
    another repository or an issue that isn't open, and ``IssueLookupError``
    when the answer names no repository.
    """
    issue_repository = _repository_of(issue.get("repository_url"))
    if issue_repository is None:
        msg = (
            f"GitHub's answer for #{number} names no repository, so it can't be "
            f"confirmed as an issue in {repository}."
        )
        raise IssueLookupError(msg)
    if issue_repository.lower() != repository.lower() or issue.get("number") != number:
        msg = (
            f"#{number} is not an issue in {repository}: it moved to "
            f"{issue_repository}#{issue.get('number')}."
        )
        raise NotAnOpenIssueError(msg)
    if issue.get("pull_request") is not None:
        msg = f"#{number} is a pull request, not an issue."
        raise NotAnOpenIssueError(msg)
    state = issue.get("state")
    if state != "open":
        msg = f"Issue #{number} is {state}."
        raise NotAnOpenIssueError(msg, state=str(state))


def require_open_issue(repository: str, number: int) -> None:
    """Return quietly if ``number`` is an open issue, not a pull request, here.

    Raise ``NotAnOpenIssueError`` or ``IssueLookupError`` otherwise; see
    ``fetch_issue`` and ``check_issue``.
    """
    check_issue(repository, number, fetch_issue(repository, number))
