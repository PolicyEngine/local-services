"""The scheduled relock: its workflow and the tools/ scripts it runs.

Each script test runs the script with fake git, gh and date first on PATH.
"""

import runpy

import pytest
import yaml
from fake_commands import (
    REPOSITORY_ROOT,
    WORKFLOWS_DIRECTORY,
    FakeCommands,
    github_output,
    run_script,
)

PREPARE_BRANCH = "relock-prepare-branch.sh"
COMMIT_AND_PUSH = "relock-commit-and-push.sh"
OPEN_PULL_REQUEST = "relock-open-pull-request.sh"
RELOCK_WORKFLOW = "update-dependencies.yaml"
REPOSITORY = "PolicyEngine/microcosm-emitter"
REMOTE_HEAD = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture
def fakes(tmp_path):
    return FakeCommands.create(tmp_path)


def relock_workflow():
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(
        (WORKFLOWS_DIRECTORY / RELOCK_WORKFLOW).read_text(), Loader=yaml.BaseLoader
    )


def relock_steps():
    return relock_workflow()["jobs"]["relock"]["steps"]


def step_running(command):
    (step,) = [step for step in relock_steps() if step.get("run") == command]
    return step


# --- The workflow --------------------------------------------------------


def test_the_relock_runs_weekly_and_by_hand_only():
    triggers = relock_workflow()["on"]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert [entry["cron"] for entry in triggers["schedule"]] == ["0 6 * * 1"]


def test_the_relock_runs_the_tested_scripts_around_uv_lock_upgrade():
    runs = [step["run"] for step in relock_steps() if "run" in step]

    assert runs == [
        f"bash tools/{PREPARE_BRANCH}",
        "uv lock --upgrade",
        f"bash tools/{COMMIT_AND_PUSH}",
        f"bash tools/{OPEN_PULL_REQUEST}",
    ]


def test_the_relock_passes_the_recorded_head_and_the_app_token_to_the_scripts():
    assert step_running(f"bash tools/{PREPARE_BRANCH}")["id"] == "branch"
    assert "env" not in step_running(f"bash tools/{PREPARE_BRANCH}")
    push = step_running(f"bash tools/{COMMIT_AND_PUSH}")
    assert push["id"] == "push"
    assert push["env"] == {"REMOTE_HEAD": "${{ steps.branch.outputs.remote_head }}"}
    open_pull_request = step_running(f"bash tools/{OPEN_PULL_REQUEST}")
    # It runs whenever the update changed files, even when the branch already
    # held them, so an open pull request keeps naming an open issue.
    assert open_pull_request["if"] == "steps.push.outputs.changes_detected == 'true'"
    assert open_pull_request["env"] == {
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}"
    }


def test_only_the_app_token_step_reads_secrets():
    """The App token, unlike GITHUB_TOKEN, starts CI on the pull request it opens."""
    workflow = relock_workflow()
    assert workflow["permissions"] == {"contents": "read"}
    steps = relock_steps()
    token = steps[0]
    assert token["id"] == "app-token"
    assert token["uses"].startswith("actions/create-github-app-token@")
    assert token["with"] == {
        "app-id": "${{ secrets.APP_ID }}",
        "private-key": "${{ secrets.APP_PRIVATE_KEY }}",
    }
    text = (WORKFLOWS_DIRECTORY / RELOCK_WORKFLOW).read_text()
    assert text.count("secrets.") == 2
    checkout = steps[1]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "ref": "main",
        "fetch-depth": "0",
        "token": "${{ steps.app-token.outputs.token }}",
    }


# --- relock-prepare-branch.sh ---------------------------------------------

# The fake git reports the remote branch's head from FAKE_REMOTE_HEAD (unset:
# the branch does not exist) and answers the other commands from the
# environment.
FAKE_GIT = """\
case "$1" in
  ls-remote)
    if [[ -n "${FAKE_REMOTE_HEAD:-}" ]]; then
      printf '%s\\trefs/heads/update-dependencies\\n' "${FAKE_REMOTE_HEAD}"
    fi
    exit "${FAKE_GIT_LS_REMOTE_EXIT:-0}"
    ;;
  status) printf '%s' "${FAKE_GIT_STATUS:-}" ;;
  diff) exit "${FAKE_GIT_DIFF_EXIT:-1}" ;;
  push) exit "${FAKE_GIT_PUSH_EXIT:-0}" ;;
esac
"""
GIT_IDENTITY = [
    ["git", "config", "user.name", "github-actions[bot]"],
    [
        "git",
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    ],
]
REMOTE_BRANCH_LOOKUP = [
    "git",
    "ls-remote",
    "--heads",
    "origin",
    "refs/heads/update-dependencies",
]
REBUILD_FROM_MAIN = ["git", "checkout", "-B", "update-dependencies", "origin/main"]


def prepare_branch(fakes, tmp_path, **env):
    output = tmp_path / "github-output"
    fakes.add("git", FAKE_GIT)
    result = run_script(PREPARE_BRANCH, env=fakes.env(GITHUB_OUTPUT=str(output), **env))
    return result, output


def test_prepare_branch_rebuilds_the_branch_from_main_when_it_exists(fakes, tmp_path):
    """Merging main into an older update branch would mix two uv.lock resolutions."""
    result, output = prepare_branch(fakes, tmp_path, FAKE_REMOTE_HEAD=REMOTE_HEAD)

    assert result.returncode == 0, result.stderr
    assert fakes.calls("git") == [
        *GIT_IDENTITY,
        REMOTE_BRANCH_LOOKUP,
        ["git", "fetch", "--quiet", "origin", REMOTE_HEAD],
        REBUILD_FROM_MAIN,
    ]
    assert github_output(output) == {"remote_head": REMOTE_HEAD}


def test_prepare_branch_starts_from_main_without_an_update_branch(fakes, tmp_path):
    result, output = prepare_branch(fakes, tmp_path)

    assert result.returncode == 0, result.stderr
    assert fakes.calls("git") == [
        *GIT_IDENTITY,
        REMOTE_BRANCH_LOOKUP,
        REBUILD_FROM_MAIN,
    ]
    assert github_output(output) == {"remote_head": ""}


def test_prepare_branch_fails_when_the_branch_lookup_fails(fakes, tmp_path):
    result, output = prepare_branch(fakes, tmp_path, FAKE_GIT_LS_REMOTE_EXIT="128")

    assert result.returncode != 0
    assert github_output(output) == {}
    assert not any(call[1] == "checkout" for call in fakes.calls("git"))


def test_prepare_branch_requires_the_output_file(fakes):
    fakes.add("git", FAKE_GIT)
    environment = fakes.env()
    environment.pop("GITHUB_OUTPUT", None)

    result = run_script(PREPARE_BRANCH, env=environment)

    assert result.returncode != 0
    assert "GITHUB_OUTPUT is required" in result.stderr
    assert fakes.calls() == []


# --- relock-commit-and-push.sh --------------------------------------------

# Every pull request adds a changelog fragment; the relock's never changes, so
# the push still compares only the update with the remote branch.
RELOCK_FRAGMENT = "changelog.d/update-dependencies.changed.md"
RELOCK_FRAGMENT_TEXT = (
    "Updated the locked dependencies in `uv.lock` to the newest versions the "
    "requirements allow.\n"
)
# Records which fragments exist when git add stages the update.
FAKE_GIT_RECORDING_FRAGMENTS = """\
if [[ "$1" == "add" ]]; then
  ls changelog.d > "$FAKE_FRAGMENTS_AT_ADD" 2>/dev/null || true
fi
"""


def commit_and_push(fakes, tmp_path, git_body=FAKE_GIT, **env):
    """Run the script in an empty checkout folder, which it may write a fragment to."""
    output = tmp_path / "github-output"
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    fakes.add("git", git_body)
    fakes.add("date", "echo 2026-10-13\n")
    result = run_script(
        COMMIT_AND_PUSH,
        env=fakes.env(GITHUB_OUTPUT=str(output), **env),
        cwd=checkout,
    )
    return result, output


def test_commit_and_push_replaces_the_branch_with_a_lease_on_the_recorded_head(
    fakes, tmp_path
):
    result, output = commit_and_push(
        fakes, tmp_path, REMOTE_HEAD=REMOTE_HEAD, FAKE_GIT_STATUS=" M uv.lock\n"
    )

    assert result.returncode == 0, result.stderr
    assert github_output(output) == {
        "changes_detected": "true",
        "branch_pushed": "true",
    }
    git_calls = fakes.calls("git")
    assert git_calls == [
        ["git", "status", "--porcelain"],
        ["git", "add", "--all"],
        ["git", "commit", "-m", "Update locked dependencies (2026-10-13)"],
        ["git", "diff", "--quiet", REMOTE_HEAD, "HEAD", "--"],
        [
            "git",
            "push",
            f"--force-with-lease=refs/heads/update-dependencies:{REMOTE_HEAD}",
            "origin",
            "HEAD:refs/heads/update-dependencies",
        ],
    ]
    assert fakes.calls("date") == [["date", "-u", "+%Y-%m-%d"]]


def test_commit_and_push_adds_a_releasable_changelog_fragment(fakes, tmp_path):
    fragments_at_add = tmp_path / "fragments-at-add"
    result, _output = commit_and_push(
        fakes,
        tmp_path,
        git_body=FAKE_GIT_RECORDING_FRAGMENTS + FAKE_GIT,
        REMOTE_HEAD=REMOTE_HEAD,
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_FRAGMENTS_AT_ADD=str(fragments_at_add),
    )

    assert result.returncode == 0, result.stderr
    changelog = tmp_path / "checkout" / "changelog.d"
    assert (changelog / "update-dependencies.changed.md").read_text() == (
        RELOCK_FRAGMENT_TEXT
    )
    assert fragments_at_add.read_text() == "update-dependencies.changed.md\n"
    bump_version = runpy.run_path(str(REPOSITORY_ROOT / "tools/bump-version.py"))
    assert bump_version["fragment_types"](changelog) == {"changed"}


def test_commit_and_push_adds_no_fragment_without_an_update(fakes, tmp_path):
    result, _output = commit_and_push(fakes, tmp_path, REMOTE_HEAD=REMOTE_HEAD)

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "checkout" / "changelog.d").exists()


def test_commit_and_push_creates_the_branch_with_a_plain_push(fakes, tmp_path):
    result, output = commit_and_push(
        fakes, tmp_path, REMOTE_HEAD="", FAKE_GIT_STATUS=" M uv.lock\n"
    )

    assert result.returncode == 0, result.stderr
    assert github_output(output)["branch_pushed"] == "true"
    assert fakes.calls("git")[3:] == [
        ["git", "push", "origin", "HEAD:refs/heads/update-dependencies"]
    ]


def test_commit_and_push_skips_the_push_when_the_branch_holds_the_update(
    fakes, tmp_path
):
    """The commit's date differs every run, so the push compares files."""
    result, output = commit_and_push(
        fakes,
        tmp_path,
        REMOTE_HEAD=REMOTE_HEAD,
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_GIT_DIFF_EXIT="0",
    )

    assert result.returncode == 0, result.stderr
    assert "update-dependencies already holds this update" in result.stdout
    assert github_output(output) == {
        "changes_detected": "true",
        "branch_pushed": "false",
    }
    assert not any(call[1] == "push" for call in fakes.calls("git"))


@pytest.mark.parametrize("remote_head", [REMOTE_HEAD, ""])
def test_commit_and_push_does_nothing_when_the_relock_changes_nothing(
    fakes, tmp_path, remote_head
):
    """Pushing main's own commit would leave an open pull request with no changes."""
    result, output = commit_and_push(fakes, tmp_path, REMOTE_HEAD=remote_head)

    assert result.returncode == 0, result.stderr
    assert "uv.lock is already up to date" in result.stdout
    assert github_output(output) == {
        "changes_detected": "false",
        "branch_pushed": "false",
    }
    assert fakes.calls("git") == [["git", "status", "--porcelain"]]


def test_commit_and_push_rejects_a_remote_head_that_is_not_a_commit(fakes, tmp_path):
    result, output = commit_and_push(fakes, tmp_path, REMOTE_HEAD="main")

    assert result.returncode == 1
    assert "REMOTE_HEAD must be empty or a full commit hash" in result.stderr
    assert github_output(output) == {}
    assert fakes.calls() == []


def test_commit_and_push_fails_when_the_push_fails(fakes, tmp_path):
    result, output = commit_and_push(
        fakes,
        tmp_path,
        REMOTE_HEAD=REMOTE_HEAD,
        FAKE_GIT_STATUS=" M uv.lock\n",
        FAKE_GIT_PUSH_EXIT="1",
    )

    assert result.returncode != 0
    assert github_output(output) == {}


# --- relock-open-pull-request.sh ------------------------------------------

# The fake gh answers each call from the environment and writes each
# description it is given to FAKE_PR_BODY_LOG.
FAKE_GH = """\
case "$1 $2" in
  "pr list") printf '%s' "${FAKE_OPEN_PR:-}" ;;
  "pr view") printf '%s' "${FAKE_OPEN_PR_BODY:-}" ;;
  "pr create" | "pr edit") cat >> "$FAKE_PR_BODY_LOG" ;;
  "issue create") printf '%s\\n' "${FAKE_CREATED_ISSUE_URL:-}" ;;
  "api --paginate")
    printf '%s' "${FAKE_OPEN_ISSUES:-}"
    exit "${FAKE_GH_API_EXIT:-0}"
    ;;
esac
"""
PULL_REQUEST_SUMMARY = (
    "Automated weekly relock by `.github/workflows/update-dependencies.yaml`. "
    "`uv lock --upgrade` updates `uv.lock` to the newest versions "
    "`pyproject.toml` allows, so this pull request changes only `uv.lock` and "
    "adds a changelog fragment. CI on this pull request tests the upgraded "
    "versions.\n\n"
    "Each run rebuilds `update-dependencies` from `main` and replaces it, so "
    "commits pushed to this branch by hand are lost on the next run."
)
OPEN_PULL_REQUEST_LOOKUP = [
    "gh",
    "pr",
    "list",
    "--repo",
    REPOSITORY,
    "--base",
    "main",
    "--head",
    "update-dependencies",
    "--state",
    "open",
    "--json",
    "number,isCrossRepository",
    "--jq",
    "map(select(.isCrossRepository | not)) | .[0].number // empty",
]
OPEN_TRACKING_ISSUE_LOOKUP = [
    "gh",
    "api",
    "--paginate",
    f"repos/{REPOSITORY}/issues?state=open&per_page=100",
    "--jq",
    '.[] | select(.pull_request == null and .title == "Update locked dependencies")'
    " | .number",
]
CREATE_PULL_REQUEST = [
    "gh",
    "pr",
    "create",
    "--repo",
    REPOSITORY,
    "--title",
    "Update locked dependencies",
    "--body-file",
    "-",
    "--base",
    "main",
    "--head",
    "update-dependencies",
]


def open_pull_request(fakes, tmp_path, **env):
    """Run the script; return its result and the descriptions it set."""
    body_log = tmp_path / "pr-bodies"
    fakes.add("gh", FAKE_GH)
    result = run_script(
        OPEN_PULL_REQUEST,
        env=fakes.env(
            GITHUB_REPOSITORY=REPOSITORY, FAKE_PR_BODY_LOG=str(body_log), **env
        ),
    )
    descriptions = body_log.read_text() if body_log.exists() else ""
    return result, descriptions


def test_open_pull_request_names_the_open_tracking_issue(fakes, tmp_path):
    result, descriptions = open_pull_request(fakes, tmp_path, FAKE_OPEN_ISSUES="870\n")

    assert result.returncode == 0, result.stderr
    assert fakes.calls() == [
        OPEN_PULL_REQUEST_LOOKUP,
        OPEN_TRACKING_ISSUE_LOOKUP,
        CREATE_PULL_REQUEST,
    ]
    assert descriptions == f"Fixes #870\n\n{PULL_REQUEST_SUMMARY}\n"


def test_open_pull_request_names_the_oldest_open_tracking_issue(fakes, tmp_path):
    result, descriptions = open_pull_request(
        fakes, tmp_path, FAKE_OPEN_ISSUES="875\n870\n1001\n"
    )

    assert result.returncode == 0, result.stderr
    assert descriptions.startswith("Fixes #870\n\n")


def test_open_pull_request_opens_a_tracking_issue_when_none_is_open(fakes, tmp_path):
    result, descriptions = open_pull_request(
        fakes,
        tmp_path,
        FAKE_CREATED_ISSUE_URL=f"https://github.com/{REPOSITORY}/issues/871",
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls()[2] == [
        "gh",
        "issue",
        "create",
        "--repo",
        REPOSITORY,
        "--title",
        "Update locked dependencies",
        "--body",
        "Tracks the automated weekly relock from update-dependencies.yaml. The "
        "relock pull request names this issue on the first line of its "
        "description and closes it when it merges.",
    ]
    assert fakes.calls()[3] == CREATE_PULL_REQUEST
    assert descriptions == f"Fixes #871\n\n{PULL_REQUEST_SUMMARY}\n"


def test_open_pull_request_fails_when_the_new_issue_has_no_number(fakes, tmp_path):
    result, descriptions = open_pull_request(
        fakes, tmp_path, FAKE_CREATED_ISSUE_URL="could not create the issue"
    )

    assert result.returncode == 1
    assert "Could not resolve the relock tracking issue" in result.stderr
    assert CREATE_PULL_REQUEST not in fakes.calls()
    assert descriptions == ""


def test_open_pull_request_fails_when_the_issue_lookup_fails(fakes, tmp_path):
    result, descriptions = open_pull_request(fakes, tmp_path, FAKE_GH_API_EXIT="1")

    assert result.returncode == 1
    assert fakes.calls() == [OPEN_PULL_REQUEST_LOOKUP, OPEN_TRACKING_ISSUE_LOOKUP]
    assert descriptions == ""


def test_open_pull_request_rewrites_the_description_of_the_open_one(fakes, tmp_path):
    """An open pull request whose issue was closed names the open one instead."""
    result, descriptions = open_pull_request(
        fakes,
        tmp_path,
        FAKE_OPEN_PR="90",
        FAKE_OPEN_PR_BODY=f"Fixes #860\n\n{PULL_REQUEST_SUMMARY}",
        FAKE_OPEN_ISSUES="870\n",
    )

    assert result.returncode == 0, result.stderr
    assert fakes.calls()[2:] == [
        [
            "gh",
            "pr",
            "view",
            "90",
            "--repo",
            REPOSITORY,
            "--json",
            "body",
            "--jq",
            ".body",
        ],
        ["gh", "pr", "edit", "90", "--repo", REPOSITORY, "--body-file", "-"],
    ]
    assert descriptions == f"Fixes #870\n\n{PULL_REQUEST_SUMMARY}\n"


def test_open_pull_request_leaves_a_current_description(fakes, tmp_path):
    result, descriptions = open_pull_request(
        fakes,
        tmp_path,
        FAKE_OPEN_PR="90",
        FAKE_OPEN_PR_BODY=f"Fixes #870\n\n{PULL_REQUEST_SUMMARY}",
        FAKE_OPEN_ISSUES="870\n",
    )

    assert result.returncode == 0, result.stderr
    assert not any(
        call[1:3] in (["pr", "edit"], ["pr", "create"]) for call in fakes.calls()
    )
    assert descriptions == ""


def test_open_pull_request_requires_the_repository(fakes):
    fakes.add("gh")
    environment = fakes.env()
    environment.pop("GITHUB_REPOSITORY", None)

    result = run_script(OPEN_PULL_REQUEST, env=environment)

    assert result.returncode != 0
    assert "GITHUB_REPOSITORY is required" in result.stderr
    assert fakes.calls() == []
