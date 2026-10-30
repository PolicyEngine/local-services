"""The scheduled relock: its workflow and the tools/ scripts it runs.

The script tests run each script in a real temporary git repository whose
origin is a local bare repository, with fake gh and date first on PATH. The
fake gh evaluates each --jq expression with jq, over JSON the test gives it,
as gh would over GitHub's answer.
"""

import json
import runpy
import subprocess

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
COMMIT = "relock-commit.sh"
OPEN_PULL_REQUEST = "relock-open-pull-request.sh"
RELOCK_WORKFLOW = "update-dependencies.yaml"
REPOSITORY = "PolicyEngine/microcosm-emitter"
BRANCH = "bot/update-dependencies/2026-11-02-060012"
EARLIER_BRANCH = "bot/update-dependencies/2026-10-26-060009"
RELOCK_COMMIT = "Update locked dependencies (2026-11-02)"


@pytest.fixture
def fakes(tmp_path):
    fakes = FakeCommands.create(tmp_path)
    fakes.add(
        "date",
        'if [[ "$*" == *%H* ]]; then echo 2026-11-02-060012; else echo 2026-11-02; fi\n',
    )
    return fakes


def git(directory, *arguments):
    return subprocess.run(
        ["git", "-C", str(directory), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A clone of a local bare origin whose main holds uv.lock, as checkout makes."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    work = tmp_path / "checkout"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    git(work, "config", "user.name", "Someone")
    git(work, "config", "user.email", "someone@example.com")
    (work / "uv.lock").write_text("locked = 1\n")
    (work / "changelog.d").mkdir()
    (work / "changelog.d/.gitkeep").touch()
    git(work, "add", "--all")
    git(work, "commit", "-q", "-m", "Start")
    git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    git(work, "config", "--unset", "user.name")
    git(work, "config", "--unset", "user.email")
    return work


def relock_workflow():
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(
        (WORKFLOWS_DIRECTORY / RELOCK_WORKFLOW).read_text(), Loader=yaml.BaseLoader
    )


def steps_of(job):
    return relock_workflow()["jobs"][job]["steps"]


def step_running(job, command):
    (step,) = [step for step in steps_of(job) if step.get("run") == command]
    return step


# --- The workflow --------------------------------------------------------


def test_the_relock_runs_weekly_and_by_hand_only():
    triggers = relock_workflow()["on"]

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert [entry["cron"] for entry in triggers["schedule"]] == ["0 6 * * 1"]


def test_the_relock_job_holds_no_token_while_uv_lock_runs_build_code():
    """uv lock may build a dependency's source archive, running its code."""
    workflow = relock_workflow()
    assert workflow["permissions"] == {"contents": "read"}
    relock = workflow["jobs"]["relock"]
    assert "permissions" not in relock
    steps = relock["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0]["with"] == {"ref": "main", "persist-credentials": "false"}
    assert [step["run"] for step in steps if "run" in step] == ["uv lock --upgrade"]
    upload = steps[-1]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "relocked-uv-lock"
    assert upload["with"]["path"] == "uv.lock"
    assert "secrets." not in yaml.safe_dump(relock)


def test_the_proposing_job_commits_before_it_creates_the_app_token():
    proposal = relock_workflow()["jobs"]["propose"]
    assert proposal["needs"] == "relock"
    steps = proposal["steps"]
    assert steps[0]["with"] == {"ref": "main", "persist-credentials": "false"}
    download = steps[1]
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"name": "relocked-uv-lock", "path": "."}
    assert [step.get("run") or step["id"] for step in steps[2:]] == [
        f"bash tools/{PREPARE_BRANCH}",
        f"bash tools/{COMMIT}",
        "app-token",
        f"bash tools/{OPEN_PULL_REQUEST}",
    ]
    assert step_running("propose", f"bash tools/{PREPARE_BRANCH}")["id"] == "branch"
    assert step_running("propose", f"bash tools/{COMMIT}")["id"] == "commit"


def test_the_app_token_is_narrowed_and_only_its_step_reads_secrets():
    token = next(step for step in steps_of("propose") if step.get("id") == "app-token")
    assert token["uses"].startswith("actions/create-github-app-token@")
    assert token["if"] == "steps.commit.outputs.changes_detected == 'true'"
    assert token["with"] == {
        "app-id": "${{ secrets.APP_ID }}",
        "private-key": "${{ secrets.APP_PRIVATE_KEY }}",
        "permission-contents": "write",
        "permission-pull-requests": "write",
        # To open the tracking issue when none is open.
        "permission-issues": "write",
    }
    assert (WORKFLOWS_DIRECTORY / RELOCK_WORKFLOW).read_text().count("secrets.") == 2
    proposal = step_running("propose", f"bash tools/{OPEN_PULL_REQUEST}")
    assert proposal["if"] == "steps.commit.outputs.changes_detected == 'true'"
    assert proposal["env"] == {
        "BRANCH": "${{ steps.branch.outputs.branch }}",
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
    }


# --- relock-prepare-branch.sh ---------------------------------------------


def prepare_branch(fakes, checkout, **env):
    output = checkout.parent / "github-output"
    result = run_script(
        PREPARE_BRANCH, env=fakes.env(GITHUB_OUTPUT=str(output), **env), cwd=checkout
    )
    return result, github_output(output)


def test_prepare_branch_starts_a_branch_named_for_this_run_from_main(fakes, checkout):
    main = git(checkout, "rev-parse", "HEAD")

    result, output = prepare_branch(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert output == {"branch": BRANCH}
    assert git(checkout, "branch", "--show-current") == BRANCH
    assert git(checkout, "rev-parse", "HEAD") == main
    assert git(checkout, "config", "user.name") == "github-actions[bot]"


def test_prepare_branch_requires_the_output_file(fakes, checkout):
    environment = fakes.env()
    environment.pop("GITHUB_OUTPUT", None)

    result = run_script(PREPARE_BRANCH, env=environment, cwd=checkout)

    assert result.returncode != 0
    assert "GITHUB_OUTPUT is required" in result.stderr


# --- relock-commit.sh -----------------------------------------------------

RELOCK_FRAGMENT_TEXT = (
    "Updated the locked dependencies in `uv.lock` to the newest versions the "
    "requirements allow.\n"
)


def commit(fakes, checkout):
    output = checkout.parent / "github-output"
    result = run_script(COMMIT, env=fakes.env(GITHUB_OUTPUT=str(output)), cwd=checkout)
    return result, github_output(output)


def test_commit_records_the_relock_with_a_releasable_fragment(fakes, checkout):
    prepare_branch(fakes, checkout)
    (checkout / "uv.lock").write_text("locked = 2\n")

    result, output = commit(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert output == {"branch": BRANCH, "changes_detected": "true"}
    assert git(checkout, "log", "-1", "--format=%s") == RELOCK_COMMIT
    assert git(checkout, "show", "--name-only", "--format=", "HEAD").split() == [
        "changelog.d/update-dependencies.changed.md",
        "uv.lock",
    ]
    assert git(checkout, "status", "--porcelain") == ""
    changelog = checkout / "changelog.d"
    assert (changelog / "update-dependencies.changed.md").read_text() == (
        RELOCK_FRAGMENT_TEXT
    )
    bump_version = runpy.run_path(str(REPOSITORY_ROOT / "tools/bump-version.py"))
    assert bump_version["fragment_types"](changelog) == {"changed"}


def test_commit_does_nothing_when_the_relock_changes_nothing(fakes, checkout):
    prepare_branch(fakes, checkout)
    before = git(checkout, "rev-parse", "HEAD")

    result, output = commit(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert "uv.lock is already up to date" in result.stdout
    assert output["changes_detected"] == "false"
    assert git(checkout, "rev-parse", "HEAD") == before
    assert not (checkout / "changelog.d/update-dependencies.changed.md").exists()


# --- relock-open-pull-request.sh ------------------------------------------

# The fake gh runs each --jq expression over the JSON in FAKE_ISSUES_JSON or
# FAKE_PULL_REQUESTS_JSON, writes the description it is given to
# FAKE_PR_BODY, and prints the URLs a test sets.
FAKE_GH = """\
arguments=("$@")
expression=""
for ((index = 0; index < ${#arguments[@]}; index++)); do
  if [[ "${arguments[index]}" == "--jq" ]]; then
    expression="${arguments[index + 1]}"
  fi
done
case "$1 $2" in
  "api --paginate")
    jq -r "${expression}" <<<"${FAKE_ISSUES_JSON:-[]}"
    exit "${FAKE_GH_API_EXIT:-0}"
    ;;
  "issue create") printf '%s\\n' "${FAKE_CREATED_ISSUE_URL:-}" ;;
  "pr list") jq -r "${expression}" <<<"${FAKE_PULL_REQUESTS_JSON:-[]}" ;;
  "pr create")
    cat > "${FAKE_PR_BODY}"
    printf 'https://github.com/%s/pull/%s\\n' "${GITHUB_REPOSITORY}" 90
    ;;
esac
"""
SUMMARY = (
    "Automated weekly relock by `.github/workflows/update-dependencies.yaml`. "
    "`uv lock --upgrade` updates `uv.lock` to the newest versions "
    "`pyproject.toml` allows, so this pull request changes only `uv.lock` and "
    "adds a changelog fragment. CI on this pull request tests the upgraded "
    "versions.\n\n"
    "Each run opens a new relock pull request from a branch of its own, and "
    "closes the earlier ones unless someone pushed to their branches."
)
TRACKING_ISSUE = {"number": 870, "title": "Update locked dependencies"}
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
    BRANCH,
]


def relock_pull_request(number, branch, *headlines, cross_repository=False):
    """GitHub's answer for an open pull request into main, as gh pr list prints."""
    return {
        "number": number,
        "headRefName": branch,
        "isCrossRepository": cross_repository,
        "commits": [{"messageHeadline": headline} for headline in headlines],
    }


def open_pull_request(fakes, checkout, issues=(TRACKING_ISSUE,), pulls=(), **env):
    """Relock on a new branch, then run the script; return its result and body."""
    prepare_branch(fakes, checkout)
    (checkout / "uv.lock").write_text("locked = 2\n")
    commit(fakes, checkout)
    body = checkout.parent / "pr-body"
    fakes.add("gh", FAKE_GH)
    environment = {
        "GITHUB_REPOSITORY": REPOSITORY,
        "BRANCH": BRANCH,
        "GH_TOKEN": "synthetic-token",
        "FAKE_PR_BODY": str(body),
        "FAKE_ISSUES_JSON": json.dumps(list(issues)),
        "FAKE_PULL_REQUESTS_JSON": json.dumps(list(pulls)),
        **env,
    }
    result = run_script(OPEN_PULL_REQUEST, env=fakes.env(**environment), cwd=checkout)
    return result, body.read_text() if body.exists() else ""


def remote_branch(checkout, branch):
    return git(checkout.parent / "origin.git", "rev-parse", f"refs/heads/{branch}")


def test_open_pull_request_pushes_the_new_branch_and_names_the_tracking_issue(
    fakes, checkout
):
    result, body = open_pull_request(fakes, checkout)

    assert result.returncode == 0, result.stderr
    assert remote_branch(checkout, BRANCH) == git(checkout, "rev-parse", "HEAD")
    assert fakes.calls("gh")[-1] == CREATE_PULL_REQUEST
    assert body == f"Fixes #870\n\n{SUMMARY}\n"


def test_open_pull_request_opens_a_tracking_issue_when_none_is_open(fakes, checkout):
    result, body = open_pull_request(
        fakes,
        checkout,
        issues=[{"number": 12, "title": "Something else"}],
        FAKE_CREATED_ISSUE_URL=f"https://github.com/{REPOSITORY}/issues/871",
    )

    assert result.returncode == 0, result.stderr
    assert [
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
    ] in fakes.calls("gh")
    assert body.startswith("Fixes #871\n\n")


def test_open_pull_request_names_the_oldest_open_tracking_issue(fakes, checkout):
    issues = [
        {"number": 875, "title": "Update locked dependencies"},
        {**TRACKING_ISSUE},
        {"number": 860, "title": "Update locked dependencies", "pull_request": {}},
    ]

    result, body = open_pull_request(fakes, checkout, issues=issues)

    assert result.returncode == 0, result.stderr
    assert body.startswith("Fixes #870\n\n")


def test_open_pull_request_closes_earlier_relocks_nobody_pushed_to(fakes, checkout):
    pulls = [
        relock_pull_request(
            80, EARLIER_BRANCH, "Update locked dependencies (2026-10-26)"
        ),
        relock_pull_request(
            81,
            "bot/update-dependencies/2026-10-19-060010",
            "Update locked dependencies (2026-10-19)",
            "Pin the release that broke the build",
        ),
        relock_pull_request(
            82, EARLIER_BRANCH, "Update locked dependencies (x)", cross_repository=True
        ),
        relock_pull_request(83, "claude/other-work", "Update locked dependencies (x)"),
    ]

    result, body = open_pull_request(fakes, checkout, pulls=pulls)

    assert result.returncode == 0, result.stderr
    assert body == (
        f"Fixes #870\n\n{SUMMARY}\n\n"
        "Earlier relock pull requests stay open, because their branches hold "
        "commits the relock did not make: #81.\n"
    )
    closes = [call for call in fakes.calls("gh") if call[1:3] == ["pr", "close"]]
    assert closes == [
        [
            "gh",
            "pr",
            "close",
            "80",
            "--repo",
            REPOSITORY,
            "--comment",
            "Superseded by #90, which relocks uv.lock from the current main.",
        ]
    ]


def test_open_pull_request_never_replaces_an_existing_branch(fakes, checkout):
    """A plain push fails rather than discard commits on a branch of that name."""
    git(checkout, "checkout", "-q", "-b", "someone")
    git(
        checkout,
        "-c",
        "user.name=Someone",
        "-c",
        "user.email=someone@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "Someone's work",
    )
    git(checkout, "push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}")
    someones_work = remote_branch(checkout, BRANCH)
    git(checkout, "checkout", "-q", "main")
    git(checkout, "branch", "-q", "-D", "someone")

    result, body = open_pull_request(fakes, checkout)

    assert result.returncode != 0
    assert remote_branch(checkout, BRANCH) == someones_work
    assert CREATE_PULL_REQUEST not in fakes.calls("gh")
    assert body == ""


def test_open_pull_request_fails_when_the_issue_lookup_fails(fakes, checkout):
    result, body = open_pull_request(fakes, checkout, FAKE_GH_API_EXIT="1")

    assert result.returncode != 0
    assert CREATE_PULL_REQUEST not in fakes.calls("gh")
    assert body == ""


def test_open_pull_request_fails_when_the_new_issue_has_no_number(fakes, checkout):
    result, body = open_pull_request(
        fakes, checkout, issues=[], FAKE_CREATED_ISSUE_URL="could not create it"
    )

    assert result.returncode == 1
    assert "Could not resolve the relock tracking issue" in result.stderr
    assert body == ""


@pytest.mark.parametrize("variable", ["GITHUB_REPOSITORY", "BRANCH"])
def test_open_pull_request_requires_its_environment(fakes, checkout, variable):
    result, body = open_pull_request(fakes, checkout, **{variable: ""})

    assert result.returncode != 0
    assert f"{variable} is required" in result.stderr
    assert fakes.calls("gh") == []


def test_open_pull_request_refuses_a_branch_the_relock_does_not_name(fakes, checkout):
    result, _body = open_pull_request(fakes, checkout, BRANCH="main")

    assert result.returncode != 0
    assert "is not a relock branch" in result.stderr
    assert fakes.calls("gh") == []
