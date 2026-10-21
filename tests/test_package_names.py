"""Package metadata and installed entry points use the requested public names."""

import tomllib
from importlib.metadata import distribution, entry_points
from importlib.util import find_spec
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NAME = "microcosm-emitter"


INTERFACE_NAME = "microcosm-emitter-interface"
INTERFACE_ROOT = ROOT / "packages" / INTERFACE_NAME
SOURCE_URL = "https://github.com/PolicyEngine/microcosm-emitter"


def test_emitter_requires_telemetry_and_its_own_interface_release():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = config["project"]
    assert project["name"] == EXPECTED_NAME
    assert project["urls"]["Source"] == SOURCE_URL
    assert "optional-dependencies" not in project
    assert set(project["dependencies"]) == {
        "alembic>=1.13.3,<2",
        "sqlalchemy>=2,<3",
        "huggingface-hub>=0.20,<2",
        "psutil>=6,<8",
        "httpx>=0.27,<1",
        # The emitter runs the checks and fixtures of the same release.
        f"{INTERFACE_NAME}=={project['version']}",
    }


def test_interface_is_the_only_other_distribution_and_has_no_dependencies():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "packages").glob("*/pyproject.toml")
    ) == [f"packages/{INTERFACE_NAME}/pyproject.toml"]
    assert config["tool"]["uv"]["workspace"] == {
        "members": [f"packages/{INTERFACE_NAME}"]
    }
    assert config["tool"]["uv"]["sources"] == {INTERFACE_NAME: {"workspace": True}}
    interface = tomllib.loads((INTERFACE_ROOT / "pyproject.toml").read_text())[
        "project"
    ]
    assert interface["name"] == INTERFACE_NAME
    assert interface["version"] == config["project"]["version"]
    assert interface["dependencies"] == []
    assert interface["urls"]["Source"] == SOURCE_URL


def test_installed_distribution_includes_host_telemetry_and_migrations():
    emitter = distribution(EXPECTED_NAME)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert emitter.version == project["version"]
    assert emitter.metadata.get_all("Project-URL") == [
        "Source, https://github.com/PolicyEngine/microcosm-emitter"
    ]
    requirements = emitter.requires
    assert all("extra ==" not in requirement for requirement in requirements)
    assert not any("microcosm-provider" in requirement for requirement in requirements)
    assert f"{INTERFACE_NAME}=={project['version']}" in requirements
    assert distribution(INTERFACE_NAME).version == project["version"]
    modules = list(entry_points(group="microcosm.emitter.modules", name="telemetry"))
    assert len(modules) == 1
    assert modules[0].value == "microcosm_emitter.telemetry.service:create_module"
    graphs = list(entry_points(group="microcosm.emitter.modules", name="orrery"))
    assert len(graphs) == 1
    assert graphs[0].value == "microcosm_emitter.orrery.service:create_module"
    assert find_spec("microcosm_emitter.orrery.client") is not None
    assert find_spec("microcosm_emitter.host.__main__") is not None
    assert find_spec("microcosm_emitter.telemetry.client") is not None
    assert (
        find_spec("microcosm_emitter.core.alembic.versions.v20261007_01_initial_spool")
        is not None
    )


def test_installed_distribution_provides_the_emitter_command():
    """`uv tool install microcosm-emitter` puts this command on the user's PATH."""
    commands = list(entry_points(group="console_scripts", name="microcosm-emitter"))
    assert len(commands) == 1
    assert commands[0].value == "microcosm_emitter.cli:main"
    assert commands[0].dist.name == EXPECTED_NAME
