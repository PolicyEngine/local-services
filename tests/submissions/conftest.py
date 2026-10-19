"""A loopback fake collector, and the Hugging Face module wired to it."""

import os
from types import SimpleNamespace

import pytest
from fake_collector import FakeCollector
from fake_hugging_face import PRIVATE_REPOSITORY, FakeHuggingFaceApi


class ManualClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def collector():
    fake = FakeCollector().start()
    try:
        yield fake
    finally:
        fake.stop()


def start_module(tmp_path, collector, **configuration):
    from microcosm_emitter.host.contracts import ModuleContext
    from microcosm_emitter.huggingface.service.module import HuggingFaceModule

    hub = FakeHuggingFaceApi(
        whoami={"auth": {"accessToken": {"role": "write"}}, "name": "fixture"}
    )
    hub.create_repository(PRIVATE_REPOSITORY, {"README.md": b"private fixture"})
    clock = ManualClock()
    available_api = {"value": hub}
    module = HuggingFaceModule(
        {
            "handover_database_path": str(tmp_path / "events.handovers.sqlite3"),
            **configuration,
        },
        ModuleContext(os.getpid()),
        api_factory=lambda: available_api["value"],
        clock=clock,
        collector_credential=lambda: "hf_synthetic",
        # Each test runs the delivery cycles itself, on its own thread.
        background_workers=False,
    )
    module.initialize()
    return SimpleNamespace(
        module=module,
        clock=clock,
        hub=hub,
        collector=collector,
        tmp_path=tmp_path,
        remove_credential=lambda: available_api.update(value=None),
        restore_credential=lambda: available_api.update(value=hub),
    )


@pytest.fixture
def submitting_module(tmp_path, collector):
    """The module with collector submissions switched on, as for development."""
    controls = start_module(
        tmp_path,
        collector,
        collector_submissions=True,
        development_collector_url=collector.origin,
    )
    try:
        yield controls
    finally:
        controls.module.close(0)


@pytest.fixture
def default_module(tmp_path, collector):
    """The module as production starts it: collector submissions off."""
    controls = start_module(tmp_path, collector)
    try:
        yield controls
    finally:
        controls.module.close(0)
