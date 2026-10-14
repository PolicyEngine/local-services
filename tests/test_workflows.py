"""Validate release ordering, credential scope, and script-only shell logic."""

import re
import runpy
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = "Update microcosm-emitter"
VERSIONED_FILE = "pyproject.toml"


def workflow_paths():
    return sorted((ROOT / ".github/workflows").glob("*.y*ml"))


def workflow(name):
    # BaseLoader keeps YAML's 'on' key a string, matching GitHub's parser.
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


@pytest.fixture
def bump_version():
    return runpy.run_path(str(ROOT / "tools/bump-version.py"))


def release_tree(root, *fragments):
    shutil.copy(ROOT / VERSIONED_FILE, root / VERSIONED_FILE)
    (root / "changelog.d").mkdir()
    (root / "changelog.d/.gitkeep").touch()
    for fragment in fragments:
        (root / "changelog.d" / fragment).write_text("A change.\n")
    return root


def remove_version_line(pyproject_text):
    return re.sub(
        r'^version = "[^"]+"\n', "", pyproject_text, count=1, flags=re.MULTILINE
    )


def add_tool_version_line(pyproject_text):
    return pyproject_text + '\n[tool.example]\nversion = "9.9.9"\n'


def test_shell_logic_lives_in_scripts():
    for path in workflow_paths():
        config = workflow(path.name)
        for job in config["jobs"].values():
            for step in job.get("steps", []):
                if "run" in step:
                    assert "\n" not in step["run"].strip(), (path, step)


def test_pull_requests_require_every_check_and_a_changelog_fragment():
    config = workflow("pr.yaml")
    assert "pull_request" in config["on"]
    jobs = config["jobs"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["changelog"]["steps"][-1]["run"] == "bash tools/check-changelog.sh"
    required = jobs["required"]
    assert required["name"] == "Required checks"
    assert required["if"] == "always()"
    assert set(required["needs"]) == {"lint-workflows", "changelog", "verify"}


def test_verification_includes_real_collector_and_matrix():
    config = workflow("ci.yml")
    assert set(config["on"]) == {"workflow_call"}
    jobs = config["jobs"]
    assert set(jobs) == {"quality", "tests", "build-packages", "collector"}
    assert jobs["tests"]["strategy"]["matrix"] == {
        "os": ["ubuntu-latest", "macos-latest"],
        "python": ["3.13", "3.14"],
    }
    assert jobs["collector"]["needs"] == "build-packages"
    assert jobs["collector"]["env"]["TEST_DATABASE_URL"].endswith("/telemetry_test")


def test_release_commit_is_verified_then_tagged_then_published():
    config = workflow("push.yaml")
    assert config["on"]["push"]["branches"] == ["main"]
    jobs = config["jobs"]
    assert set(jobs) == {"versioning", "verify", "tag", "publish"}
    assert f"!= '{RELEASE_COMMIT}'" in jobs["versioning"]["if"]
    assert jobs["versioning"]["steps"][-2]["run"] == "bash tools/update-version.sh"
    assert jobs["versioning"]["steps"][-1]["with"]["message"] == RELEASE_COMMIT
    assert f"== '{RELEASE_COMMIT}'" in jobs["verify"]["if"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["tag"]["needs"] == "verify"
    assert jobs["tag"]["steps"][-1]["run"] == "bash tools/publish-git-tag.sh"
    assert jobs["publish"]["needs"] == "tag"


def test_only_publication_holds_oidc_and_only_versioning_holds_secrets():
    config = workflow("push.yaml")
    assert "id-token" not in config["permissions"]
    jobs = config["jobs"]
    oidc_jobs = {
        name for name, job in jobs.items() if "id-token" in job.get("permissions", {})
    }
    assert oidc_jobs == {"publish"}
    assert jobs["publish"]["environment"] == "pypi"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    text = (ROOT / ".github/workflows/push.yaml").read_text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"APP_ID", "APP_PRIVATE_KEY"}
    assert text.count("secrets.") == 2


def test_pypi_receives_only_verified_artifacts():
    publish = workflow("push.yaml")["jobs"]["publish"]
    assert publish["steps"][0]["with"]["name"] == "distributions"
    assert not any("run" in step for step in publish["steps"])


@pytest.mark.parametrize(
    ("types", "expected"),
    [
        ({"fixed"}, "1.4.3"),
        ({"fixed", "changed"}, "1.4.3"),
        ({"fixed", "added"}, "1.5.0"),
        ({"removed"}, "1.5.0"),
        ({"added", "breaking"}, "2.0.0"),
    ],
)
def test_fragment_types_choose_the_bump(bump_version, types, expected):
    assert bump_version["next_version"]("1.4.2", types) == expected


def test_release_rewrites_only_the_version_line(tmp_path, bump_version):
    root = release_tree(tmp_path, "1.added.md", "repair.fixed.md")
    current, upcoming = bump_version["plan_release"](root)
    assert upcoming == bump_version["next_version"](current, {"added", "fixed"})
    bump_version["write_version"](root, current, upcoming)
    assert bump_version["current_version"](root) == upcoming
    original = (ROOT / VERSIONED_FILE).read_text().splitlines()
    updated = (root / VERSIONED_FILE).read_text().splitlines()
    assert len(updated) == len(original)
    changed = [
        (original_line, updated_line)
        for original_line, updated_line in zip(original, updated, strict=True)
        if original_line != updated_line
    ]
    assert changed == [(f'version = "{current}"', f'version = "{upcoming}"')]


@pytest.mark.parametrize(
    "fragments", [[], ["1.feature.md"], ["1.added.rst"], ["notes.md"]]
)
def test_release_refuses_missing_or_untyped_fragments(
    tmp_path, bump_version, fragments
):
    root = release_tree(tmp_path, *fragments)
    with pytest.raises(ValueError, match="changelog fragment"):
        bump_version["plan_release"](root)


@pytest.mark.parametrize(
    "edit_pyproject",
    [remove_version_line, add_tool_version_line],
    ids=["missing", "duplicated"],
)
def test_release_refuses_a_missing_or_duplicated_version_line(
    tmp_path, bump_version, edit_pyproject
):
    root = release_tree(tmp_path, "1.fixed.md")
    pyproject = root / VERSIONED_FILE
    pyproject.write_text(edit_pyproject(pyproject.read_text()))
    with pytest.raises(ValueError, match="one version line"):
        bump_version["plan_release"](root)


def test_build_verifies_one_distribution_without_a_workspace():
    script = (ROOT / "tools/check-artifacts.sh").read_text()
    assert script.count("uv build ") == 1
    assert "--package" not in script
    assert "--clear" in script
    for path in workflow_paths():
        assert "--all-packages" not in path.read_text()
