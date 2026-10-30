"""Check that Python packages are locked, installed and tested from uv.lock.

The guard reads every tracked file that can install Python packages or run
tests: workflows, composite actions, shell scripts, Makefiles, Dockerfiles,
Compose files and Python modules that define Modal images. It checks four rules,
whose ids are the registry's ``rule_prefix`` followed by the names below:

- every project directory commits a uv.lock, and a job of the pull-request
  workflow, or of a reusable workflow of this repository that it calls, runs
  ``uv lock --check``;
- every install comes from uv.lock (``uv sync --frozen``, ``uv run --frozen``,
  Modal ``uv_sync(frozen=True)``), unless a ``[[rule.exception]]`` names the site;
- every test command runs through ``uv run --frozen``;
- a scheduled workflow relocks every uv.lock with upgrades and opens a PR.

Workflow commands are followed into the shell scripts they call in the
registry's ``script_directories`` and into root Makefile targets.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from scripts.rules.load import load_registry
from scripts.rules.model import Violation, normalized_package
from scripts.rules.workflows import called_workflow, parse_jobs

if TYPE_CHECKING:
    from collections.abc import Iterable

    from scripts.rules.model import Registry, Repository, RuleException

REPO_ROOT = Path(__file__).resolve().parents[2]

# Each rule's id is the registry's rule_prefix, a dot, and one of these names.
LOCK_RULE = "dependencies.lock-committed"
INSTALL_RULE = "dependencies.install-from-lock"
TEST_RULE = "dependencies.test-from-lock"
RELOCK_RULE = "dependencies.scheduled-relock"
PINNED_TOOL = "pinned-tool"
# Fixture trees hold example files, never this repository's own installs.
FIXTURE_DIRECTORY = "fixtures"

_LOCKED_FLAG = re.compile(r"(?<![\w-])--(frozen|locked)(?![\w-])")
_UNLOCKED_RUN_FLAG = re.compile(r"(?<![\w-])--(with|with-requirements|no-project)\b")
# An exact pin: name==version or name@version, where the version is a release
# number or a variable that holds one (such as $(RUFF_VERSION)), never a moving
# name like latest.
_PINNED = re.compile(
    r"[\w${}.\[\],-]+(?:==|@)(?:\d[\w.+-]*|\$\{\w+\}|\$\(\w+\))(?![\w${}()-])"
)
_UNLOCKED_INSTALLS = (
    ("uv pip", re.compile(r"\buv\s+pip\s+(install|sync)\b")),
    ("uv tool", re.compile(r"\buv\s+tool\s+(install|run)\b")),
    ("uvx", re.compile(r"(?<![\w/-])uvx\s")),
    ("uv add", re.compile(r"\buv\s+add\b")),
    ("pip", re.compile(r"(?<![\w-])(pip3?|python3?\s+-m\s+pip)\s+install\b")),
    ("pipx", re.compile(r"(?<![\w-])pipx\s+(install|run)\b")),
    ("poetry", re.compile(r"(?<![\w-])poetry\s+install\b")),
    ("conda", re.compile(r"(?<![\w-])(conda|mamba|micromamba)\s+install\b")),
)
_UV_SYNC = re.compile(r"\buv\s+(?:--[\w-]+(?:[= ]\S+)?\s+)*sync\b")
_UV_RUN = re.compile(r"\buv\s+(?:--[\w-]+(?:[= ]\S+)?\s+)*run\b")
_TEST_COMMAND = re.compile(
    r"(?<![\w.-])pytest(?![\w.-])|\bcoverage\s+run\b|-m\s+unittest\b"
)
# A command that only prints or edits text installs nothing, even when the text
# names an installer: the first program after any assignment, $(shell or
# shell keyword decides.
_TEXT_TOOL = re.compile(
    r"^\s*@?(?:[\w.-]+\s*[:?]?=\s*)?(?:\$\((?:shell\s+)?)?"
    r"(?:(?:then|do|else|if|!)\s+)*(echo|printf|sed|grep|awk)\b"
)
_SEPARATORS = re.compile(r"&&|\|\||;|\||`")
_MAKE_TARGET = re.compile(r"(?<![\w-])make\s+([\w-]+)")
_MAKE_HEADER = re.compile(r"^([\w.-]+)\s*:(?!=)")
_RELOCK = re.compile(
    r"\buv\s+lock\b[^\n]*?(?<![\w-])(--upgrade|--upgrade-package|-U|-P)(?![\w-])"
)
_OPENS_PR = re.compile(r"\bgh\s+pr\s+(create|edit)\b|create-pull-request")
_LOCK_CHECK = re.compile(r"\buv\s+lock\b[^\n]*--check\b")
_MODAL_INSTALLS = (
    "pip_install",
    "pip_install_from_requirements",
    "pip_install_from_pyproject",
    "pip_install_private_repos",
    "poetry_install_from_file",
    "uv_pip_install",
    "conda_install",
    "micromamba_install",
)
_MODAL_CALL = re.compile(
    r"\.(" + "|".join((*_MODAL_INSTALLS, "uv_sync", "run_commands")) + r")\("
)
_MODAL_FROZEN = re.compile(r"\bfrozen\s*=\s*True\b")


@dataclass(frozen=True)
class Site:
    """One command or call that installs packages or runs tests."""

    path: str
    line: int
    text: str

    def __str__(self) -> str:
        """Format as ``path:line: text``."""
        return f"{self.path}:{self.line}: {self.text}"


@dataclass
class Findings:
    """What the scan found, before exceptions are applied."""

    scanned: set[str] = field(default_factory=set)
    unlocked_installs: list[Site] = field(default_factory=list)
    unlocked_tests: list[Site] = field(default_factory=list)


def tracked_files(root: Path) -> list[str]:
    """Tracked files under ``root``, or every file when it is not a git checkout."""
    if (root / ".git").exists():
        listed = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return [path for path in listed.splitlines() if (root / path).is_file()]
    return sorted(
        str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()
    )


def _is_fixture_or_test(path: str, test_directories: Iterable[str]) -> bool:
    """Whether ``path`` is under a fixtures directory or a test directory."""
    parts = PurePosixPath(path).parts
    return FIXTURE_DIRECTORY in parts or any(
        directory in parts for directory in test_directories
    )


def file_kind(path: str, test_directories: Iterable[str] = ()) -> str | None:
    """Return which kind of install-capable file ``path`` is, or None.

    Nothing under a fixtures directory counts, and Python modules under one of
    ``test_directories`` don't either.
    """
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if FIXTURE_DIRECTORY in pure.parts:
        return None
    kinds = (
        (
            "workflow",
            path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")),
        ),
        (
            "action",
            path.startswith(".github/actions/")
            and name in {"action.yml", "action.yaml"},
        ),
        ("shell", name.endswith(".sh")),
        ("makefile", name == "makefile" or name.endswith(".mk")),
        ("dockerfile", "dockerfile" in name and not name.endswith(".dockerignore")),
        ("compose", fnmatch.fnmatch(name, "*compose*.y*ml")),
        (
            "python",
            name.endswith(".py") and not _is_fixture_or_test(path, test_directories),
        ),
    )
    return next((kind for kind, matches in kinds if matches), None)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations and drop comment-only lines."""
    joined: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not pending and stripped.startswith("#"):
            continue
        if not pending:
            start = number
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        joined.append((start, pending + stripped))
        pending = ""
    if pending:
        joined.append((start, pending))
    return joined


def _commands(text: str) -> list[tuple[int, str]]:
    """Split each logical line into the simple commands it runs."""
    commands = []
    for number, logical in _logical_lines(text):
        for part in _SEPARATORS.split(logical):
            command = part.replace('"', "").replace("'", "").strip()
            if command and not _TEXT_TOOL.match(command):
                commands.append((number, command))
    return commands


def _workflow_commands(text: str) -> list[tuple[int, str]]:
    """Return each workflow run-block command with the file line it is on.

    Run blocks are found in file order: each block starts on the first line,
    after the previous block's start, that holds the block's first line.
    """
    jobs, _problems = parse_jobs(text)
    lines = text.splitlines()
    commands = []
    cursor = 0
    for job in jobs.values():
        for block in job.run_blocks:
            block_lines = block.text.splitlines()
            first = next((line.strip() for line in block_lines if line.strip()), "")
            start = next(
                (
                    index
                    for index in range(cursor, len(lines))
                    if first and first in lines[index]
                ),
                cursor,
            )
            cursor = start + 1
            offset = next(
                (index for index, line in enumerate(block_lines) if line.strip()), 0
            )
            commands.extend(
                (start + 1 + number - 1 - offset, command)
                for number, command in _commands(block.text)
            )
    return commands


def _shell_text(kind: str, text: str) -> list[tuple[int, str]]:
    """Return the shell code a file runs, with the line it starts on."""
    if kind == "workflow":
        return _workflow_commands(text)
    if kind == "dockerfile":
        return [
            (number, command)
            for number, logical in _logical_lines(text)
            if re.match(r"(?i)^RUN\s", logical)
            for _, command in _commands(logical[4:])
        ]
    return _commands(text)


def _unlocked_install(command: str) -> bool:
    if any(pattern.search(command) for _, pattern in _UNLOCKED_INSTALLS):
        return True
    if _UV_SYNC.search(command):
        return not _LOCKED_FLAG.search(command)
    if _UV_RUN.search(command):
        return not _LOCKED_FLAG.search(command) or bool(
            _UNLOCKED_RUN_FLAG.search(command)
        )
    return False


def _unlocked_test(command: str) -> bool:
    if not _TEST_COMMAND.search(command):
        return False
    return not (_UV_RUN.search(command) and _LOCKED_FLAG.search(command))


def _calls(text: str) -> list[tuple[int, str, str]]:
    """Return each Modal image call as (line, method, call text)."""
    calls = []
    for match in _MODAL_CALL.finditer(text):
        depth = 0
        end = match.end() - 1
        for index in range(match.end() - 1, len(text)):
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        number = text.count("\n", 0, match.start()) + 1
        call_text = text[match.start() + 1 : end].replace('"', "").replace("'", "")
        call = " ".join(call_text.split())
        calls.append((number, match.group(1), call))
    return calls


def _scan_python(path: str, text: str, findings: Findings) -> None:
    for number, method, call in _calls(text):
        unlocked = (
            method in _MODAL_INSTALLS
            or (method == "uv_sync" and not _MODAL_FROZEN.search(call))
            or (method == "run_commands" and _unlocked_install(call))
        )
        if unlocked:
            findings.unlocked_installs.append(Site(path, number, call))
        if method == "run_commands" and _unlocked_test(call):
            findings.unlocked_tests.append(Site(path, number, call))


def scan(
    root: Path,
    files: Iterable[str] | None = None,
    test_directories: Iterable[str] = (),
) -> Findings:
    """Scan every install-capable file under ``root``.

    Python modules under ``test_directories`` are not scanned.
    """
    findings = Findings()
    directories = tuple(test_directories)
    for path in sorted(tracked_files(root) if files is None else files):
        kind = file_kind(path, directories)
        if kind is None:
            continue
        findings.scanned.add(path)
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        if kind == "python":
            _scan_python(path, text, findings)
            continue
        for number, command in _shell_text(kind, text):
            if _unlocked_install(command):
                findings.unlocked_installs.append(Site(path, number, command))
            if _unlocked_test(command):
                findings.unlocked_tests.append(Site(path, number, command))
    return findings


def _covers(exception: RuleException, site: Site) -> bool:
    return (
        fnmatch.fnmatch(site.path, exception.path)
        and bool(exception.match)
        and (exception.match in site.text)
    )


def _exception_problems(rule_id: str, exception: RuleException) -> list[Violation]:
    """Return why an exception does not name a site, a reason and an end."""
    problems = []
    where = f"exception for {exception.path}"
    if not exception.match:
        problems.append(
            Violation(
                rule_id,
                "locked-installs",
                f"{where} names no site",
                "add match = a substring of the one command or call it allows",
            )
        )
    if exception.category == PINNED_TOOL:
        if not _PINNED.search(exception.match):
            problems.append(
                Violation(
                    rule_id,
                    "locked-installs",
                    f"pinned-tool {where} does not pin an exact version",
                    "pin the tool with == and include the pin in match",
                )
            )
    elif exception.category:
        problems.append(
            Violation(
                rule_id,
                "locked-installs",
                f"{where} has unknown category '{exception.category}'",
                f"use category = '{PINNED_TOOL}' or leave it out",
            )
        )
    elif not exception.removal and (
        exception.issue is None or exception.expires == date.max
    ):
        problems.append(
            Violation(
                rule_id,
                "locked-installs",
                f"{where} has no removal condition and no review date with a "
                "tracking issue",
                "add removal = the condition for removing it, or both expires "
                "and issue",
            )
        )
    return problems


def _apply_exceptions(
    registry: Registry, rule_id: str, sites: list[Site], what: str, fix: str
) -> list[Violation]:
    rule = next((rule for rule in registry.rules if rule.id == rule_id), None)
    exceptions = rule.exceptions if rule is not None else ()
    violations = [
        problem
        for exception in exceptions
        for problem in _exception_problems(rule_id, exception)
    ]
    violations.extend(
        Violation(rule_id, "locked-installs", f"{what}: {site}", fix)
        for site in sites
        if not any(_covers(exception, site) for exception in exceptions)
    )
    violations.extend(
        Violation(
            rule_id,
            "locked-installs",
            f"exception for {exception.path} ({exception.match}) matches no site",
            "remove the stale exception",
        )
        for exception in exceptions
        if exception.match and not any(_covers(exception, site) for site in sites)
    )
    return violations


def _makefile_recipes(root: Path, makefile: str) -> dict[str, list[str]]:
    recipes: dict[str, list[str]] = {}
    path = root / makefile
    if not path.is_file():
        return recipes
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        header = _MAKE_HEADER.match(raw)
        if header is not None and not header.group(1).startswith("."):
            current = header.group(1)
            recipes[current] = []
        elif raw.startswith("\t") and current is not None:
            recipes[current].append(raw)
        elif raw.strip() and not raw.startswith("\t"):
            current = None
    return recipes


def script_reference(script_directories: Iterable[str]) -> re.Pattern[str]:
    """Match a call of a shell script in one of ``script_directories``."""
    directories = "|".join(
        re.escape(directory.strip("/")) for directory in sorted(script_directories)
    )
    return re.compile(rf"(?<![\w/.-])(?:\./)?((?:{directories})/[\w./-]+\.sh)")


def executed_text(
    root: Path,
    text: str,
    script_directories: Iterable[str],
    seen: set[str] | None = None,
) -> str:
    """Return ``text`` plus the scripts and root Make targets it runs.

    Only scripts in ``script_directories`` are followed.
    """
    seen = set() if seen is None else seen
    directories = tuple(script_directories)
    recipes = _makefile_recipes(root, "Makefile")
    pieces = [text]
    calls = script_reference(directories).findall(text) if directories else []
    for script in calls:
        if script not in seen and (root / script).is_file():
            seen.add(script)
            script_text = (root / script).read_text(encoding="utf-8")
            pieces.append(executed_text(root, script_text, directories, seen))
    for target in _MAKE_TARGET.findall(text):
        if f"make {target}" not in seen and target in recipes:
            seen.add(f"make {target}")
            recipe = "\n".join(recipes[target])
            pieces.append(executed_text(root, recipe, directories, seen))
    return "\n".join(pieces)


def _project_directories(
    files: list[str], test_directories: Iterable[str]
) -> list[str]:
    """Return the directories of the projects, leaving out fixtures and tests."""
    directories = tuple(test_directories)
    return sorted(
        str(PurePosixPath(path).parent)
        for path in files
        if PurePosixPath(path).name == "pyproject.toml"
        and not _is_fixture_or_test(path, directories)
    )


def _workflow_jobs(
    root: Path, files: list[str], script_directories: Iterable[str]
) -> list[tuple[str, str, str]]:
    """Return (workflow, job id, executed text) for every workflow job."""
    jobs = []
    for path in files:
        if file_kind(path) != "workflow":
            continue
        workflow_text = (root / path).read_text(encoding="utf-8")
        parsed, _problems = parse_jobs(workflow_text)
        for job_id, job in parsed.items():
            run_text = "\n".join(block.text for block in job.run_blocks)
            uses = "\n".join(job.step_uses)
            executed = executed_text(root, f"{run_text}\n{uses}", script_directories)
            jobs.append((path, job_id, executed))
    return jobs


def pull_request_workflows(root: Path, pr_workflow: str) -> set[str]:
    """Return the pull-request workflow and the local workflows it calls.

    Reusable workflows called with ``uses: ./...`` are followed, through
    further calls, as long as the called file exists.
    """
    found: set[str] = set()
    pending = [normalized_package(pr_workflow)]
    while pending:
        workflow = pending.pop()
        if workflow in found or not (root / workflow).is_file():
            continue
        found.add(workflow)
        jobs, _problems = parse_jobs((root / workflow).read_text(encoding="utf-8"))
        pending.extend(
            called
            for job in jobs.values()
            if (called := called_workflow(job.uses)) is not None
        )
    return found


def lock_violations(
    root: Path, files: list[str], repository: Repository
) -> list[Violation]:
    """Rule 1: each project commits a uv.lock and the PR workflow checks it."""
    rule_id = repository.rule_id(LOCK_RULE)
    pr_workflow = repository.pr_workflow
    tracked = set(files)
    violations = [
        Violation(
            rule_id,
            "locked-installs",
            f"{directory}/pyproject.toml has no committed uv.lock",
            f"run uv lock --project {directory} and commit uv.lock",
        )
        for directory in _project_directories(files, repository.test_directories)
        if f"{directory}/uv.lock".removeprefix("./") not in tracked
    ]
    pull_request = pull_request_workflows(root, pr_workflow)
    checked = any(
        normalized_package(workflow) in pull_request and _LOCK_CHECK.search(text)
        for workflow, _job, text in _workflow_jobs(
            root, files, repository.script_directories
        )
    )
    if not checked:
        violations.append(
            Violation(
                rule_id,
                "locked-installs",
                f"no {pr_workflow} job runs uv lock --check",
                f"run uv lock --check in a job of {pr_workflow}",
            )
        )
    return violations


def relock_violations(
    root: Path, files: list[str], repository: Repository
) -> list[Violation]:
    """Rule 4: a scheduled workflow relocks every uv.lock and opens a PR."""
    rule_id = repository.rule_id(RELOCK_RULE)
    projects = _project_directories(files, repository.test_directories)
    for path in files:
        if file_kind(path) != "workflow":
            continue
        if not re.search(
            r"^\s+schedule:", (root / path).read_text(encoding="utf-8"), re.MULTILINE
        ):
            continue
        for workflow, _job, text in _workflow_jobs(
            root, [path], repository.script_directories
        ):
            if not (_RELOCK.search(text) and _OPENS_PR.search(text)):
                continue
            globs = re.findall(r"[\w./*-]*pyproject\.toml", text)
            uncovered = [
                directory
                for directory in projects
                if directory != "."
                and not any(
                    fnmatch.fnmatch(f"{directory}/pyproject.toml", glob)
                    for glob in globs
                )
            ]
            if not uncovered:
                return []
            return [
                Violation(
                    rule_id,
                    "locked-installs",
                    f"{workflow} does not relock {directory}/uv.lock",
                    "relock every project directory in the scheduled job",
                )
                for directory in uncovered
            ]
    return [
        Violation(
            rule_id,
            "locked-installs",
            "no scheduled workflow relocks uv.lock with upgrades and opens a PR",
            "add a schedule: workflow that runs uv lock --upgrade and gh pr create",
        )
    ]


def find_violations(root: Path, registry: Registry) -> list[Violation]:
    """Run all four checks against the files under ``root``."""
    files = tracked_files(root)
    repository = registry.repository
    findings = scan(root, files, repository.test_directories)
    return [
        *lock_violations(root, files, repository),
        *_apply_exceptions(
            registry,
            repository.rule_id(INSTALL_RULE),
            findings.unlocked_installs,
            "unlocked install",
            "install from uv.lock (uv sync --frozen, uv run --frozen, "
            "uv_sync(frozen=True)) or register a [[rule.exception]] for the site",
        ),
        *_apply_exceptions(
            registry,
            repository.rule_id(TEST_RULE),
            findings.unlocked_tests,
            "test command outside uv.lock",
            "run it through uv run --frozen or register a [[rule.exception]]",
        ),
        *relock_violations(root, files, repository),
    ]


def run_check(root: Path = REPO_ROOT) -> list[Violation]:
    """Load the registry under ``root`` and return every violation."""
    registry, violations = load_registry(root)
    if registry is None:
        return violations
    return find_violations(root, registry)


def check() -> list[str]:
    """Adapter for scripts/run_quality_guards.py."""
    return [str(violation) for violation in run_check(REPO_ROOT)]
