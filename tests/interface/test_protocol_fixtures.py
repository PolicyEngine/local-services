"""This emitter accepts every protocol fixture the interface package ships.

Builds keep whichever emitter is installed, so every emitter release must
accept every protocol version that interface releases still in use send. The
fixtures are those messages; released fixtures are never edited, only added.
"""

import os
import shutil
import time
from pathlib import Path

import pytest

from microcosm_emitter.compatibility import SUPPORTED_PROTOCOL_VERSIONS
from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.orrery.service.module import OrreryModule
from microcosm_emitter.telemetry.service.module import TelemetryModule
from microcosm_emitter_interface.launcher import DEFAULT_COMMAND, start_emitter
from microcosm_emitter_interface.protocol_fixtures import (
    fixture_digests,
    materialize,
    protocol_fixtures,
)

#: SHA-256 of each released fixture file. A changed digest means a released
#: message changed shape: add a fixture or a protocol version instead.
RELEASED_FIXTURE_DIGESTS = {
    "protocol-v1/host-hello.json": (
        "36fcac377b69a9103b7c1cfd884d2ff667e030463edc9da349a379bb4868b927"
    ),
    "protocol-v1/orrery-configuration.json": (
        "956b5e1ca4c65a9fc0282f648b45d00ae697ac72e50147269e365a52d16f141a"
    ),
    "protocol-v1/orrery-enqueue.json": (
        "e70c33e089b7361dc01e0448dd34458c8ca8c606ea8c552af191c9b08cb11c5b"
    ),
    "protocol-v1/orrery-inventory.json": (
        "8cba4ec76bec8db21f1e158bf5da4eef23ca799f6730012051280d33763092f5"
    ),
    "protocol-v1/orrery-receipt.json": (
        "cbc7ed6d1776af8082d6def5f920d547400c7da073febea89364150ccd4e7a4b"
    ),
    "protocol-v1/orrery-retry.json": (
        "acfa79ba0221ff3786e61cdad699124e81cf825cc4b1364895a3679e839ade49"
    ),
    "protocol-v1/telemetry-calibration-progress.json": (
        "52c2396dd12f9e0cb65e429139d0bd1f2fa6e5adf9c007fd513161de560775c4"
    ),
    "protocol-v1/telemetry-complete.json": (
        "8f32aa105366fc544a3dd85d3e330c8602feb2f770bb196d49d09a4126747a05"
    ),
    "protocol-v1/telemetry-configuration.json": (
        "fd2ae2786543b8d9239f4f5a6604440daada9ab89ec974abc858b4847c3a563b"
    ),
    "protocol-v1/telemetry-emit.json": (
        "0ae388f58fae80267391360c30b21bf392072eea7158dda465630911e3d919c3"
    ),
    "protocol-v1/telemetry-fail-without-code.json": (
        "ad55024a6bf0fae3583245b74b83671ea652dbb5ad48763d2e1e341c116e5fdb"
    ),
    "protocol-v1/telemetry-fail.json": (
        "f509a443a361dfa5133fa32667b88a2e6be8f0ef29f8facfc03136e1af215b56"
    ),
    "protocol-v1/telemetry-progress.json": (
        "fcb9f3b69b2c4e941b9a7f4af300a87781861d6b1b69acdeb1ba2f3fb7d52166"
    ),
    "protocol-v1/telemetry-refusal.json": (
        "33c6bc9750b9e342a8a86e7cc9c25c23a76a03419b6bdb1690bfb9ec3353c82c"
    ),
    "protocol-v1/telemetry-stage.json": (
        "083a6a88e30092b45a75911d8cd80e7f8079a7715d2a5870215203ec15c642ec"
    ),
    "protocol-v1/telemetry-transition-calibration-progress.json": (
        "9236f9bc9c0cf771fc67af01f1b99606be28a8c49d142709aeb6f3cad1bc46da"
    ),
    "protocol-v1/telemetry-transition-stage.json": (
        "50fcb5f0379579625db58daba44aab2d1a63c73c4d54738dfdf65d7b93d8c783"
    ),
}


def fixtures_of(module: str, kind: str):
    return sorted(
        (
            fixture
            for fixture in protocol_fixtures()
            if fixture.module == module and fixture.kind == kind
        ),
        key=lambda fixture: (fixture.sequence, fixture.name),
    )


def configuration_of(module: str, tmp_path: Path) -> dict:
    (fixture,) = fixtures_of(module, "configuration")
    return materialize(
        fixture, directory=tmp_path, spool_path=tmp_path / "events.sqlite3"
    )


def test_every_fixture_uses_a_protocol_version_this_emitter_accepts():
    fixtures = protocol_fixtures()
    assert fixtures
    assert {fixture.protocol_version for fixture in fixtures} <= set(
        SUPPORTED_PROTOCOL_VERSIONS
    )


def test_released_fixtures_are_unchanged():
    assert fixture_digests() == RELEASED_FIXTURE_DIGESTS


@pytest.mark.parametrize(
    "fixture",
    fixtures_of("telemetry", "message"),
    ids=lambda fixture: fixture.name,
)
def test_telemetry_module_records_each_message_fixture(tmp_path, fixture):
    module = TelemetryModule(
        configuration_of("telemetry", tmp_path), ModuleContext(os.getpid())
    )
    module.initialize()
    try:
        registration = module.registration
        before = module.spool.batch(registration["run_id"], registration["producer_id"])
        module.handle_message(
            materialize(fixture, directory=tmp_path, spool_path=tmp_path)
        )
        after = module.spool.batch(registration["run_id"], registration["producer_id"])
        assert len(after) > len(before)
    finally:
        module.close(time.monotonic())


def test_orrery_module_answers_each_query_fixture_in_order(tmp_path):
    module = OrreryModule(
        configuration_of("orrery", tmp_path), ModuleContext(os.getpid())
    )
    module.initialize()
    directory = tmp_path / "inputs"
    try:
        for fixture in fixtures_of("orrery", "query"):
            directory.mkdir(exist_ok=True)
            for name, text in fixture.files.items():
                (directory / name).write_text(text)
            response = module.query(
                materialize(fixture, directory=directory, spool_path=tmp_path)
            )
            assert set(fixture.response_keys) <= set(response), fixture.name
            receipt = response.get("receipt")
            if receipt is not None:
                assert receipt["status"] == "pending", fixture.name
    finally:
        module.close(time.monotonic())


def test_installed_command_answers_the_hello_fixture(tmp_path):
    (fixture,) = fixtures_of("host", "hello")
    process = start_emitter(
        {"orrery": configuration_of("orrery", tmp_path)},
        command=shutil.which(DEFAULT_COMMAND),
    )
    try:
        assert set(process.description) == set(fixture.response_keys)
    finally:
        process.close()
    assert process.process.wait(timeout=20) == 0
