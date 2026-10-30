"""Keep the rules tool, its registry and their documents out of the source archives.

They are development tools of this repository, not part of either published
distribution. The emitter's build leaves them out of its source archive, and
the artifact check (tools/check-installed.py, run by tools/check-artifacts.sh
in CI) refuses either source archive if they come back.
"""

import io
import runpy
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_DIRECTORY = "microcosm_emitter-0.0.0"


def artifact_check():
    return runpy.run_path(str(ROOT / "tools/check-installed.py"))


def write_archive(path, members):
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            info = tarfile.TarInfo(f"{ARCHIVE_DIRECTORY}/{member}")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))


def test_the_emitter_build_excludes_what_the_artifact_check_refuses():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    excluded = pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"]

    unpublished = artifact_check()["UNPUBLISHED_PATHS"]
    assert set(unpublished) == {
        "scripts/rules/",
        "scripts/run_quality_guards.py",
        "rules/",
        "docs/generated/rules.md",
        "docs/engineering/skills/rules-registry.md",
    }
    assert sorted(excluded) == sorted(f"/{path.rstrip('/')}" for path in unpublished)


def test_the_artifact_check_refuses_an_archive_holding_the_rules_tool(tmp_path):
    archive = tmp_path / f"{ARCHIVE_DIRECTORY}.tar.gz"
    write_archive(
        archive,
        [
            "pyproject.toml",
            "src/microcosm_emitter/__init__.py",
            "scripts/rules/model.py",
            "scripts/rules/tests/fixtures/policyengine_api_pr.yml",
            "scripts/run_quality_guards.py",
            "rules/registry.toml",
            "docs/generated/rules.md",
            "docs/engineering/skills/rules-registry.md",
            "tests/rules/test_example.py",
        ],
    )

    assert artifact_check()["unpublished_members"](archive) == [
        "docs/engineering/skills/rules-registry.md",
        "docs/generated/rules.md",
        "rules/registry.toml",
        "scripts/rules/model.py",
        "scripts/rules/tests/fixtures/policyengine_api_pr.yml",
        "scripts/run_quality_guards.py",
    ]


def test_the_artifact_check_accepts_an_archive_without_it(tmp_path):
    archive = tmp_path / f"{ARCHIVE_DIRECTORY}.tar.gz"
    write_archive(archive, ["pyproject.toml", "src/microcosm_emitter/__init__.py"])

    assert artifact_check()["unpublished_members"](archive) == []
