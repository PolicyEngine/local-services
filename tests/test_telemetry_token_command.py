"""`microcosm-emitter telemetry-token`: a collector token for a build elsewhere.

The operator's workstation runs it when it launches a build on Modal, which
has no Hugging Face credential. Only the token, its expiry and the collector
origin reach standard output; nothing about the token reaches standard error.
"""

import json
from datetime import datetime, timedelta

import pytest
from fake_collector import FakeCollector

from microcosm_emitter import cli
from microcosm_emitter.core import auth as auth_module

SYNTHETIC_HUGGING_FACE_TOKEN = "hf_synthetic_operator_token"
STAGE_LIFETIME_SECONDS = 18 * 3600
UNREACHABLE_COLLECTOR_URL = "http://127.0.0.1:1"


@pytest.fixture
def collector():
    fake = FakeCollector().start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def operator_login(monkeypatch):
    """The operator's Hugging Face login, and nothing ambient."""
    monkeypatch.delenv("HUGGINGFACE_TOKEN", raising=False)
    monkeypatch.setenv("HF_TOKEN", SYNTHETIC_HUGGING_FACE_TOKEN)
    monkeypatch.setattr(auth_module, "get_token", lambda: None)


def telemetry_token(collector_url: str, lifetime_seconds: int) -> int:
    return cli.main(
        [
            "telemetry-token",
            "--lifetime-seconds",
            str(lifetime_seconds),
            "--development-collector-url",
            collector_url,
        ]
    )


def test_prints_a_telemetry_token_for_the_requested_lifetime(
    collector, operator_login, capsys
) -> None:
    requested_at = datetime.now().astimezone()

    exit_code = telemetry_token(collector.origin, STAGE_LIFETIME_SECONDS)

    output = capsys.readouterr()
    assert exit_code == 0
    assert output.err == ""
    printed = json.loads(output.out)
    assert sorted(printed) == ["access_token", "collector_origin", "expires_at"]
    assert printed["collector_origin"] == collector.origin
    assert printed["access_token"].startswith("session-token-")
    # The expiry counts from before the request, so it never overstates it.
    expires_at = datetime.fromisoformat(printed["expires_at"])
    assert expires_at.utcoffset() == timedelta(0)
    lifetime = expires_at - requested_at
    assert timedelta(seconds=STAGE_LIFETIME_SECONDS - 2) <= lifetime
    assert lifetime <= timedelta(seconds=STAGE_LIFETIME_SECONDS)
    assert collector.exchange_requests == [
        {"scope": "telemetry:write", "lifetime_seconds": STAGE_LIFETIME_SECONDS}
    ]
    assert collector.exchange_credentials == [SYNTHETIC_HUGGING_FACE_TOKEN]


@pytest.mark.parametrize(
    ("failure", "expected_exit_code"),
    [
        ("no Hugging Face login", 3),
        ("credential refused with 401", 4),
        ("not an organization member (403)", 4),
        ("collector unreachable", 5),
        ("collector answers 503", 5),
        ("collector without lifetime_seconds (422)", 6),
    ],
)
def test_each_failure_has_its_own_exit_code_and_prints_nothing(
    collector, operator_login, monkeypatch, capsys, failure, expected_exit_code
) -> None:
    collector_url = collector.origin
    if failure == "no Hugging Face login":
        monkeypatch.delenv("HF_TOKEN")
    elif failure == "credential refused with 401":
        collector.exchange_status = 401
    elif failure == "not an organization member (403)":
        collector.exchange_status = 403
    elif failure == "collector unreachable":
        collector_url = UNREACHABLE_COLLECTOR_URL
    elif failure == "collector answers 503":
        collector.exchange_status = 503
    else:
        collector.accepts_lifetime_seconds = False

    exit_code = telemetry_token(collector_url, STAGE_LIFETIME_SECONDS)

    output = capsys.readouterr()
    assert exit_code == expected_exit_code
    assert output.out == ""
    assert output.err.strip()
    assert SYNTHETIC_HUGGING_FACE_TOKEN not in output.err
    if failure == "no Hugging Face login":
        assert collector.exchange_requests == []
