"""Validate release ordering, credential scope, and script-only shell logic."""

import re
import runpy
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
RELEASE_COMMIT = "Update microcosm-emitter"
VERSIONED_FILES = ("pyproject.toml", "package.json", "typescript/orrery/package.json")


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
    for name in VERSIONED_FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, root / name)
    (root / "changelog.d").mkdir()
    (root / "changelog.d/.gitkeep").touch()
    for fragment in fragments:
        (root / "changelog.d" / fragment).write_text("A change.\n")
    return root


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
    assert set(jobs) == {"quality", "tests", "build-packages", "collector", "reader"}
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
    assert f"!= '{RELEASE_COMMIT}'" in jobs["versioning"]["if"]
    assert jobs["versioning"]["steps"][-2]["run"] == "bash tools/update-version.sh"
    assert jobs["versioning"]["steps"][-1]["with"]["message"] == RELEASE_COMMIT
    assert f"== '{RELEASE_COMMIT}'" in jobs["verify"]["if"]
    assert jobs["verify"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["tag"]["needs"] == "verify"
    assert jobs["tag"]["steps"][-1]["run"] == "bash tools/publish-git-tag.sh"
    assert jobs["publish"]["needs"] == "tag"
    assert jobs["publish-reader"]["needs"] == ["verify", "publish"]


def test_only_publication_holds_oidc_and_only_versioning_holds_secrets():
    config = workflow("push.yaml")
    assert "id-token" not in config["permissions"]
    jobs = config["jobs"]
    oidc_jobs = {
        name for name, job in jobs.items() if "id-token" in job.get("permissions", {})
    }
    assert oidc_jobs == {"publish", "publish-reader"}
    for name in oidc_jobs:
        assert jobs[name]["environment"] == "pypi"
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    text = (ROOT / ".github/workflows/push.yaml").read_text()
    assert set(re.findall(r"secrets\.(\w+)", text)) == {"APP_ID", "APP_PRIVATE_KEY"}
    assert text.count("secrets.") == 2


def test_registries_receive_only_verified_artifacts():
    jobs = workflow("push.yaml")["jobs"]
    publish = jobs["publish"]
    assert publish["steps"][0]["with"]["name"] == "distributions"
    assert not any("run" in step for step in publish["steps"])
    reader = jobs["publish-reader"]
    download = next(
        step for step in reader["steps"] if "download-artifact@" in step.get("uses", "")
    )
    assert download["with"]["name"] == "orrery-reader"
    assert reader["steps"][-1]["run"] == "bash tools/publish-reader.sh"
    publish = (ROOT / "tools/publish-reader.sh").read_text()
    # A bare "reader-dist/<file>" is a GitHub shorthand to npm, not a path.
    assert 'npm publish --access public --provenance "./${archives[0]}"' in publish


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


def test_release_writes_one_version_to_every_package(tmp_path, bump_version):
    root = release_tree(tmp_path, "1.added.md", "repair.fixed.md")
    current, upcoming = bump_version["plan_release"](root)
    assert upcoming == bump_version["next_version"](current, {"added", "fixed"})
    bump_version["write_version"](root, current, upcoming)
    assert bump_version["current_version"](root) == upcoming
    for name in VERSIONED_FILES:
        original = (ROOT / name).read_text().splitlines()
        updated = (root / name).read_text().splitlines()
        assert len(updated) == len(original)
        changed = [
            pair for pair in zip(original, updated, strict=True) if pair[0] != pair[1]
        ]
        assert changed == [(changed[0][0], changed[0][0].replace(current, upcoming))]


@pytest.mark.parametrize(
    "fragments", [[], ["1.feature.md"], ["1.added.rst"], ["notes.md"]]
)
def test_release_refuses_missing_or_untyped_fragments(
    tmp_path, bump_version, fragments
):
    root = release_tree(tmp_path, *fragments)
    with pytest.raises(ValueError, match="changelog fragment"):
        bump_version["plan_release"](root)


def test_release_refuses_diverged_package_versions(tmp_path, bump_version):
    root = release_tree(tmp_path, "1.fixed.md")
    reader = root / "typescript/orrery/package.json"
    reader.write_text(
        re.sub(r'"version": "[^"]+"', '"version": "9.9.9"', reader.read_text(), count=1)
    )
    with pytest.raises(ValueError, match="one shared version"):
        bump_version["plan_release"](root)


def test_build_verifies_one_distribution_without_a_workspace():
    script = (ROOT / "tools/check-artifacts.sh").read_text()
    assert script.count("uv build ") == 1
    assert "--package" not in script
    assert "--clear" in script
    for path in workflow_paths():
        assert "--all-packages" not in path.read_text()
