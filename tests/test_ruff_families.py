"""Every Ruff rule family the registry ratchets still has findings.

The ruff-ignores guard reads the configuration only, so this test runs Ruff
with every rule selected except those that stay off for good, and fails when
a family the baseline lists has no finding left: the pull request that fixes
a family's last finding also takes it off the list.
"""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULE_ID = "emitter.quality.ruff-families-not-yet-fixed"


def ruff(*arguments):
    completed = subprocess.run(
        [sys.executable, "-m", "ruff", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def ratcheted_rule():
    registry = tomllib.loads((ROOT / "rules/registry.toml").read_text())
    (rule,) = [rule for rule in registry["rule"] if rule["id"] == RULE_ID]
    return rule


def listed_families(rule):
    return [
        line.strip()
        for line in (ROOT / rule["baseline"]["file"]).read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def family_prefixes():
    """Every rule family's selector, longest first, such as PLR before PL."""
    prefixes = []
    for linter in json.loads(ruff("linter", "--output-format", "json")):
        categories = linter.get("categories") or [{"prefix": ""}]
        prefixes.extend(
            linter["prefix"] + category["prefix"] for category in categories
        )
    return sorted((prefix for prefix in prefixes if prefix), key=len, reverse=True)


def family_of(code, prefixes):
    return next(
        prefix
        for prefix in prefixes
        if code.startswith(prefix) and code[len(prefix) :].isdigit()
    )


def test_every_ratcheted_ruff_family_still_has_findings():
    rule = ratcheted_rule()
    kept_off = [exception["match"] for exception in rule.get("exception", [])]
    findings = json.loads(
        ruff(
            "check",
            ".",
            # A --select on the command line replaces the configuration's
            # selection and its ignore list, so only --ignore applies.
            "--select",
            "ALL",
            "--ignore",
            ",".join(kept_off),
            "--output-format",
            "json",
            "--exit-zero",
        )
    )
    prefixes = family_prefixes()
    found = {
        family_of(finding["code"], prefixes) for finding in findings if finding["code"]
    }

    fixed = sorted(set(listed_families(rule)) - found)
    assert fixed == [], (
        f"{', '.join(fixed)} no longer has findings: remove it from "
        "[tool.ruff.lint] ignore and from the rule's baseline, and lower its count"
    )
