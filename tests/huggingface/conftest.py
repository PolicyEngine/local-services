"""A Hugging Face module driven by hand, against the in-memory Hub."""

import os
from types import SimpleNamespace

import pytest
from fake_hugging_face import (
    PRIVATE_REPOSITORY,
    STAGING_REPOSITORY,
    FakeHuggingFaceApi,
)


class ManualClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def hub():
    api = FakeHuggingFaceApi(
        whoami={"auth": {"accessToken": {"role": "write"}}, "name": "fixture"}
    )
    api.create_repository(STAGING_REPOSITORY, {"README.md": b"staging fixture"})
    api.create_repository(PRIVATE_REPOSITORY, {"README.md": b"private fixture"})
    return api


@pytest.fixture
def hugging_face_module(tmp_path, hub):
    """Start the module with an injected Hub; yields controls for the test."""
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule

    clock = ManualClock()
    available_api = {"value": hub}
    module = HuggingFaceModule(
        {"handover_database_path": str(tmp_path / "events.handovers.sqlite3")},
        ModuleContext(os.getpid()),
        api_factory=lambda: available_api["value"],
        clock=clock,
    )
    module.initialize()
    controls = SimpleNamespace(
        module=module,
        clock=clock,
        hub=hub,
        tmp_path=tmp_path,
        remove_credential=lambda: available_api.update(value=None),
        restore_credential=lambda: available_api.update(value=hub),
    )
    try:
        yield controls
    finally:
        module.close(0)
