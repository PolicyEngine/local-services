"""Tests for the issue lookup the downgrade check uses.

The lookup is part of the rules tool, so the guard needs no other script to
check that a ``[rule.downgrade]`` names an open issue. ``check_issue`` reads
GitHub's answer for a number; the tracking-issue tests run the whole lookup
through a fake ``gh``.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.rules.issues import (
    IssueLookupError,
    NotAnOpenIssueError,
    check_issue,
    require_open_issue,
)

REPOSITORY = "example/repo"
FAKE_GH_PRINTING_TEXT = "#!/bin/sh\necho 'not an issue'\n"


def answer(number: int, **fields: object) -> dict[str, object]:
    """Return the fields gh api prints for an open issue, with ``fields`` changed."""
    return {
        "number": number,
        "state": "open",
        "repository_url": f"https://api.github.com/repos/{REPOSITORY}",
        **fields,
    }


class CheckIssueTest(unittest.TestCase):
    def test_an_open_issue_in_the_repository_passes(self) -> None:
        check_issue(REPOSITORY, 912, answer(912))

    def test_the_repository_comparison_ignores_case(self) -> None:
        check_issue("Example/Repo", 912, answer(912))

    def test_a_closed_issue_names_its_state(self) -> None:
        with self.assertRaises(NotAnOpenIssueError) as raised:
            check_issue(REPOSITORY, 912, answer(912, state="closed"))

        self.assertEqual(str(raised.exception), "Issue #912 is closed.")
        self.assertEqual(raised.exception.state, "closed")

    def test_a_pull_request_is_no_issue(self) -> None:
        with self.assertRaises(NotAnOpenIssueError) as raised:
            check_issue(REPOSITORY, 912, answer(912, pull_request={"url": "x"}))

        self.assertEqual(str(raised.exception), "#912 is a pull request, not an issue.")
        self.assertIsNone(raised.exception.state)

    def test_an_issue_moved_to_another_repository_is_no_issue_here(self) -> None:
        moved = answer(5, repository_url="https://api.github.com/repos/example/other")

        with self.assertRaises(NotAnOpenIssueError) as raised:
            check_issue(REPOSITORY, 912, moved)

        self.assertEqual(
            str(raised.exception),
            "#912 is not an issue in example/repo: it moved to example/other#5.",
        )

    def test_an_answer_without_a_repository_cannot_be_confirmed(self) -> None:
        with self.assertRaises(IssueLookupError):
            check_issue(REPOSITORY, 912, answer(912, repository_url=None))


class RequireOpenIssueTest(unittest.TestCase):
    def test_an_answer_that_is_not_json_fails_the_lookup(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fake_gh = directory / "gh"
        fake_gh.write_text(FAKE_GH_PRINTING_TEXT)
        fake_gh.chmod(0o755)

        with (
            mock.patch.dict(os.environ, {"PATH": str(directory)}),
            self.assertRaises(IssueLookupError) as raised,
        ):
            require_open_issue(REPOSITORY, 912)

        self.assertEqual(
            str(raised.exception), "gh api did not print an issue for #912."
        )


if __name__ == "__main__":
    unittest.main()
