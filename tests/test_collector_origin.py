"""Every module sends the builder's Hugging Face token to one trusted origin."""

import os

import pytest

from microcosm_emitter.core import collector_origin as collector_origin_module
from microcosm_emitter.core.auth import CollectorSession
from microcosm_emitter.core.collector_origin import (
    configured_collector_origin,
    resolve_collector_origin,
)
from microcosm_emitter.core.constants import (
    COLLECTOR_URL_HTTPS_ERROR,
    COLLECTOR_URL_ORIGIN_ERROR,
    CONFLICTING_COLLECTOR_ORIGIN_ERROR,
    DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR,
    PRODUCTION_COLLECTOR_URL,
    TRUSTED_COLLECTOR_ORIGINS,
    UNTRUSTED_COLLECTOR_ORIGIN_ERROR,
)
from microcosm_emitter.host.contracts import ModuleContext
from microcosm_emitter.huggingface.service import module as huggingface_module
from microcosm_emitter.huggingface.service.module import HuggingFaceModule
from microcosm_emitter.orrery.service.module import OrreryModule
from microcosm_emitter.telemetry.client import TelemetryRun
from microcosm_emitter.telemetry.service.collector import CollectorDelivery
from microcosm_emitter.telemetry.service.module import TelemetryModule
from microcosm_emitter.telemetry.service.spool import EventSpool

# Stands in for a custom domain that a later release adds to the trusted set.
FUTURE_COLLECTOR_ORIGIN = "https://collector.example"
DEVELOPMENT_COLLECTOR_URL = "http://127.0.0.1:1"


@pytest.fixture
def future_origin_trusted(monkeypatch) -> str:
    monkeypatch.setattr(
        collector_origin_module,
        "TRUSTED_COLLECTOR_ORIGINS",
        frozenset({PRODUCTION_COLLECTOR_URL, FUTURE_COLLECTOR_ORIGIN}),
    )
    return FUTURE_COLLECTOR_ORIGIN


def telemetry_module(tmp_path, context: ModuleContext, **settings) -> TelemetryModule:
    run = TelemetryRun("origin-run", "UK", "test", producer_id="origin-producer")
    return TelemetryModule(
        {
            "registration": run.as_registration(),
            "spool_path": str(tmp_path / "events.sqlite3"),
            **settings,
        },
        context,
    )


def orrery_module(tmp_path, context: ModuleContext, **settings) -> OrreryModule:
    return OrreryModule(
        {"spool_path": str(tmp_path / "graphs.sqlite3"), **settings}, context
    )


def submitting_huggingface_module(
    tmp_path, context: ModuleContext, **settings
) -> HuggingFaceModule:
    """The Hugging Face module with collector submissions switched on.

    It reaches neither the Hub nor a collector: these tests only start it.
    """
    return HuggingFaceModule(
        {
            "handover_database_path": str(tmp_path / "events.handovers.sqlite3"),
            "collector_submissions": True,
            **settings,
        },
        context,
        api_factory=lambda: None,
        collector_credential=lambda: None,
        background_workers=False,
    )


@pytest.fixture
def submission_session_origins(monkeypatch) -> list[str]:
    """The origin of each `artifacts:submit` session the Hugging Face module opens."""
    origins: list[str] = []

    class RecordingCollectorSession(CollectorSession):
        def __init__(self, origin: str, **options) -> None:
            origins.append(origin)
            super().__init__(origin, **options)

    monkeypatch.setattr(
        huggingface_module, "CollectorSession", RecordingCollectorSession
    )
    return origins


def test_default_origin_is_the_production_collector() -> None:
    assert resolve_collector_origin() == PRODUCTION_COLLECTOR_URL
    assert configured_collector_origin({}) == PRODUCTION_COLLECTOR_URL


def test_trusted_origins_today_are_only_the_production_collector() -> None:
    assert TRUSTED_COLLECTOR_ORIGINS == frozenset({PRODUCTION_COLLECTOR_URL})
    assert resolve_collector_origin(collector_origin=PRODUCTION_COLLECTOR_URL) == (
        PRODUCTION_COLLECTOR_URL
    )


def test_every_trusted_origin_is_a_bare_https_origin() -> None:
    for trusted_origin in TRUSTED_COLLECTOR_ORIGINS:
        assert trusted_origin.startswith("https://")
        assert resolve_collector_origin(collector_origin=trusted_origin) == (
            trusted_origin
        )


def test_configured_origin_outside_the_trusted_set_is_refused() -> None:
    for untrusted_origin in (
        "https://untrusted.example",
        "https://collector.example",
        PRODUCTION_COLLECTOR_URL.replace("https://", "https://evil."),
    ):
        with pytest.raises(ValueError, match=UNTRUSTED_COLLECTOR_ORIGIN_ERROR):
            resolve_collector_origin(collector_origin=untrusted_origin)


def test_configured_origin_must_be_an_origin_even_when_its_host_is_trusted(
    future_origin_trusted,
) -> None:
    for value in (
        f"{future_origin_trusted}/v1",
        f"{future_origin_trusted}?next=1",
        future_origin_trusted.replace("https://", "https://user@"),
    ):
        with pytest.raises(ValueError, match=COLLECTOR_URL_ORIGIN_ERROR):
            resolve_collector_origin(collector_origin=value)
    with pytest.raises(ValueError, match=COLLECTOR_URL_HTTPS_ERROR):
        resolve_collector_origin(
            collector_origin=future_origin_trusted.replace("https://", "http://")
        )


def test_trusted_configured_origin_is_selected(future_origin_trusted) -> None:
    assert resolve_collector_origin(collector_origin=future_origin_trusted) == (
        future_origin_trusted
    )
    assert resolve_collector_origin(collector_origin=future_origin_trusted + "/") == (
        future_origin_trusted
    )
    assert configured_collector_origin({"collector_origin": future_origin_trusted}) == (
        future_origin_trusted
    )


def test_development_url_still_requires_loopback() -> None:
    assert resolve_collector_origin(
        development_collector_url=DEVELOPMENT_COLLECTOR_URL
    ) == (DEVELOPMENT_COLLECTOR_URL)
    with pytest.raises(ValueError, match=DEVELOPMENT_COLLECTOR_LOOPBACK_ERROR):
        resolve_collector_origin(development_collector_url="https://collector.example")


def test_development_url_and_configured_origin_cannot_both_be_set() -> None:
    with pytest.raises(ValueError, match=CONFLICTING_COLLECTOR_ORIGIN_ERROR):
        resolve_collector_origin(
            development_collector_url=DEVELOPMENT_COLLECTOR_URL,
            collector_origin=PRODUCTION_COLLECTOR_URL,
        )
    with pytest.raises(ValueError, match=CONFLICTING_COLLECTOR_ORIGIN_ERROR):
        configured_collector_origin(
            {
                "development_collector_url": DEVELOPMENT_COLLECTOR_URL,
                "collector_origin": PRODUCTION_COLLECTOR_URL,
            }
        )


def test_empty_module_settings_count_as_unset() -> None:
    assert (
        configured_collector_origin(
            {"development_collector_url": None, "collector_origin": ""}
        )
        == PRODUCTION_COLLECTOR_URL
    )


def test_environment_cannot_select_a_collector_origin(
    monkeypatch, future_origin_trusted
) -> None:
    for variable_name in (
        "MICROCOSM_COLLECTOR_ORIGIN",
        "MICROCOSM_TELEMETRY_COLLECTOR_URL",
        "COLLECTOR_ORIGIN",
    ):
        monkeypatch.setenv(variable_name, future_origin_trusted)

    assert resolve_collector_origin() == PRODUCTION_COLLECTOR_URL
    assert configured_collector_origin({}) == PRODUCTION_COLLECTOR_URL


def test_collector_delivery_sends_events_to_the_configured_origin(
    tmp_path, future_origin_trusted
) -> None:
    spool = EventSpool(tmp_path / "events.sqlite3")
    try:
        delivery = CollectorDelivery(spool, collector_origin=future_origin_trusted)

        assert delivery.collector_url == future_origin_trusted
        assert delivery.session.origin == future_origin_trusted
        assert CollectorDelivery(spool).collector_url == PRODUCTION_COLLECTOR_URL
        with pytest.raises(ValueError, match=UNTRUSTED_COLLECTOR_ORIGIN_ERROR):
            CollectorDelivery(spool, collector_origin="https://untrusted.example")
    finally:
        spool.close()


def test_telemetry_and_orrery_modules_share_the_configured_origin(
    tmp_path, future_origin_trusted
) -> None:
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(
        tmp_path, context, collector_origin=future_origin_trusted
    )
    graph = orrery_module(tmp_path, context, collector_origin=future_origin_trusted)
    telemetry.initialize()
    try:
        graph.initialize()
        try:
            session = context.services["collector_session"]
            assert session.origin == future_origin_trusted
            assert telemetry.delivery.collector_url == future_origin_trusted
            assert telemetry.delivery.session is session
        finally:
            graph.close(0)
    finally:
        telemetry.close(0)


def test_modules_without_a_setting_use_the_production_collector(tmp_path) -> None:
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(tmp_path, context)
    graph = orrery_module(tmp_path, context)
    telemetry.initialize()
    try:
        graph.initialize()
        try:
            assert context.services["collector_session"].origin == (
                PRODUCTION_COLLECTOR_URL
            )
            assert telemetry.delivery.collector_url == PRODUCTION_COLLECTOR_URL
        finally:
            graph.close(0)
    finally:
        telemetry.close(0)


@pytest.mark.usefixtures("future_origin_trusted")
@pytest.mark.parametrize(
    ("settings", "expected_origin"),
    [
        ({"development_collector_url": ""}, PRODUCTION_COLLECTOR_URL),
        (
            {
                "development_collector_url": "",
                "collector_origin": FUTURE_COLLECTOR_ORIGIN,
            },
            FUTURE_COLLECTOR_ORIGIN,
        ),
    ],
)
def test_modules_treat_an_empty_development_url_as_unset(
    tmp_path, settings, expected_origin
) -> None:
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(tmp_path, context, **settings)
    graph = orrery_module(tmp_path, context, **settings)
    telemetry.initialize()
    try:
        graph.initialize()
        try:
            assert context.services["collector_session"].origin == expected_origin
            assert telemetry.delivery.collector_url == expected_origin
        finally:
            graph.close(0)
    finally:
        telemetry.close(0)


def test_modules_refuse_an_untrusted_configured_origin(tmp_path) -> None:
    untrusted_origin = "https://untrusted.example"
    with pytest.raises(ValueError, match=UNTRUSTED_COLLECTOR_ORIGIN_ERROR):
        telemetry_module(
            tmp_path,
            ModuleContext(os.getpid()),
            collector_origin=untrusted_origin,
        ).initialize()
    with pytest.raises(ValueError, match=UNTRUSTED_COLLECTOR_ORIGIN_ERROR):
        orrery_module(
            tmp_path,
            ModuleContext(os.getpid()),
            collector_origin=untrusted_origin,
        ).initialize()


def test_collector_submissions_use_the_configured_origin(
    tmp_path, future_origin_trusted, submission_session_origins
) -> None:
    module = submitting_huggingface_module(
        tmp_path,
        ModuleContext(os.getpid()),
        collector_origin=future_origin_trusted,
    )
    module.initialize()
    try:
        assert module.collector_delivery is not None
        assert module.collector_delivery.client.origin == future_origin_trusted
        assert submission_session_origins == [future_origin_trusted]
    finally:
        module.close(0)


def test_collector_submissions_without_a_setting_use_the_production_collector(
    tmp_path, submission_session_origins
) -> None:
    module = submitting_huggingface_module(tmp_path, ModuleContext(os.getpid()))
    module.initialize()
    try:
        assert module.collector_delivery is not None
        assert module.collector_delivery.client.origin == PRODUCTION_COLLECTOR_URL
        assert submission_session_origins == [PRODUCTION_COLLECTOR_URL]
    finally:
        module.close(0)


def test_every_module_sends_to_the_same_configured_origin(
    tmp_path, future_origin_trusted, submission_session_origins
) -> None:
    context = ModuleContext(os.getpid())
    telemetry = telemetry_module(
        tmp_path, context, collector_origin=future_origin_trusted
    )
    graph = orrery_module(tmp_path, context, collector_origin=future_origin_trusted)
    hugging_face = submitting_huggingface_module(
        tmp_path, context, collector_origin=future_origin_trusted
    )
    telemetry.initialize()
    try:
        graph.initialize()
        try:
            hugging_face.initialize()
            try:
                shared_origin = context.services["collector_session"].origin
                assert shared_origin == future_origin_trusted
                assert telemetry.delivery.collector_url == shared_origin
                assert hugging_face.collector_delivery is not None
                assert hugging_face.collector_delivery.client.origin == shared_origin
                assert submission_session_origins == [shared_origin]
            finally:
                hugging_face.close(0)
        finally:
            graph.close(0)
    finally:
        telemetry.close(0)


@pytest.mark.usefixtures("future_origin_trusted")
@pytest.mark.parametrize(
    "settings",
    [
        {"collector_origin": "https://untrusted.example"},
        {
            "collector_origin": FUTURE_COLLECTOR_ORIGIN,
            "development_collector_url": DEVELOPMENT_COLLECTOR_URL,
        },
        {"collector_origin": True},
        {"collector_origin": [FUTURE_COLLECTOR_ORIGIN]},
        {"development_collector_url": 1},
    ],
    ids=[
        "untrusted origin",
        "origin and development URL",
        "origin as a boolean",
        "origin as a list",
        "development URL as a number",
    ],
)
def test_collector_submissions_refuse_an_unusable_origin_at_start(
    tmp_path, capsys, submission_session_origins, settings
) -> None:
    module = submitting_huggingface_module(
        tmp_path, ModuleContext(os.getpid()), **settings
    )
    module.initialize()
    try:
        # A collector setting never stops the Hugging Face module: it turns
        # collector submissions off and keeps the Hugging Face delivery.
        assert module.collector_delivery is None
        assert module.delivery is not None
        assert submission_session_origins == []
    finally:
        module.close(0)
    assert "collector submissions are off" in capsys.readouterr().err
    with pytest.raises(ValueError):
        configured_collector_origin(settings)
