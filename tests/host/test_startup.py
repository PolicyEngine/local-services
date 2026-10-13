"""Startup shares one deadline and reports only safe failure metadata."""

import traceback
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from microcosm_emitter.host import launcher
from microcosm_emitter.host.constants import STARTUP_TIMEOUT
from microcosm_emitter.host.errors import LocalServiceError


@pytest.fixture
def startup(tmp_path, monkeypatch):
    state = SimpleNamespace(now=0.0, ready_at=0.0, ping_delay=0.0, transfer_delay=0.0)
    directory = tmp_path / "runtime"
    process = Mock()
    process.poll.return_value = None
    process.wait.return_value = 0
    launch = Mock(return_value=process)
    writer = Mock()
    writer.__enter__ = Mock(return_value=writer)
    writer.__exit__ = Mock(return_value=False)
    writer.sendall.side_effect = lambda payload: advance(state.transfer_delay)
    reader = Mock()
    reader.__enter__ = Mock(return_value=reader)
    reader.__exit__ = Mock(return_value=False)
    timeouts = []

    def advance(seconds):
        state.now += seconds

    class Client:
        timeout = 0.2

        def __init__(self, path):
            self.socket_path = path

        def ping(self, *, timeout=None):
            timeouts.append(self.timeout if timeout is None else timeout)
            advance(state.ping_delay)
            if state.now < state.ready_at:
                raise LocalServiceError("not ready")

    def create_directory(**kwargs):
        directory.mkdir()
        return str(directory)

    monkeypatch.setattr(
        launcher, "time", SimpleNamespace(monotonic=lambda: state.now, sleep=advance)
    )
    monkeypatch.setattr(launcher.tempfile, "mkdtemp", create_directory)
    monkeypatch.setattr(
        launcher.psutil,
        "Process",
        lambda pid: SimpleNamespace(pid=pid, create_time=lambda: 1.0),
    )
    monkeypatch.setattr(launcher.subprocess, "Popen", launch)
    monkeypatch.setattr(launcher.socket, "socketpair", lambda: (reader, writer))
    monkeypatch.setattr(launcher, "SocketClient", Client)
    return SimpleNamespace(
        state=state,
        process=process,
        launch=launch,
        directory=directory,
        timeouts=timeouts,
        writer=writer,
    )


def test_shared_default_accepts_readiness_after_old_three_second_limit(startup):
    assert STARTUP_TIMEOUT == 15.0  # User-approved shared startup allowance.
    startup.state.ready_at = 3.5
    handle = launcher.start_service("test", {})
    assert 3.5 <= startup.state.now < STARTUP_TIMEOUT
    assert handle.process is startup.process
    startup.process.terminate.assert_not_called()


def test_success_after_deadline_is_not_accepted(startup):
    startup.state.ping_delay = 0.06
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.05)
    assert captured.value.reason == "readiness_timeout"
    assert startup.timeouts == [0.05]
    startup.process.terminate.assert_called_once()
    assert not startup.directory.exists()


def test_explicit_short_deadline_bounds_poll_sleep_and_reaps_child(startup):
    startup.state.ready_at = 1.0
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.03)
    assert captured.value.reason == "readiness_timeout"
    assert startup.state.now == pytest.approx(0.03)
    assert startup.timeouts == pytest.approx([0.03, 0.01])
    startup.process.terminate.assert_called_once()
    startup.process.wait.assert_called_once()
    assert not startup.directory.exists()


def test_configuration_transfer_uses_the_same_deadline(startup):
    startup.state.transfer_delay = 0.04
    startup.state.ready_at = 1.0
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.05)
    assert captured.value.reason == "readiness_timeout"
    assert startup.state.now == pytest.approx(0.05)
    assert startup.timeouts == pytest.approx([0.01])
    assert not startup.directory.exists()


def test_configuration_transfer_that_consumes_deadline_never_pings(startup):
    startup.state.transfer_delay = 0.06
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0.05)
    assert captured.value.reason == "configuration_timeout"
    assert startup.timeouts == []
    assert not startup.directory.exists()


def test_child_exit_preserves_safe_exit_code_and_cleans_directory(startup):
    startup.process.poll.return_value = 7
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {})
    assert captured.value.reason == "child_exit"
    assert captured.value.exit_code == 7
    assert "7" in str(captured.value)
    startup.process.wait.assert_called_once()
    assert not startup.directory.exists()


@pytest.mark.parametrize("timeout", [-1, float("nan"), float("inf"), "private"])
def test_invalid_deadline_starts_no_child(startup, timeout):
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=timeout)
    assert captured.value.reason == "invalid_configuration"
    startup.launch.assert_not_called()
    assert not startup.directory.exists()


def test_invalid_configuration_starts_no_child_and_has_safe_reason(startup):
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_services({"module": {"token": object()}})
    assert captured.value.reason == "invalid_configuration"
    startup.launch.assert_not_called()


def test_startup_error_and_traceback_do_not_retain_raw_exception(startup):
    startup.launch.side_effect = PermissionError("Bearer hf_synthetic_private_payload")
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {"token": "hf_synthetic_configuration"})
    assert captured.value.reason == "startup_failure"
    assert captured.value.error_type == "PermissionError"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    rendered = "".join(traceback.format_exception(captured.value))
    assert "hf_synthetic_private_payload" not in rendered
    assert "hf_synthetic_configuration" not in str(captured.value)
    assert not startup.directory.exists()


def test_custom_exception_type_name_is_not_a_startup_diagnostic(startup):
    private_error = type(
        "hf_synthetic_private_class", (Exception,), {"__module__": "builtins"}
    )
    startup.launch.side_effect = private_error("private payload")
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {})
    assert captured.value.error_type == "Exception"
    assert "hf_synthetic_private_class" not in str(captured.value)
    assert "private payload" not in "".join(traceback.format_exception(captured.value))


def test_zero_deadline_never_transfers_configuration_and_still_reaps(startup):
    with pytest.raises(LocalServiceError) as captured:
        launcher.start_service("test", {}, startup_timeout=0)
    assert captured.value.reason == "configuration_timeout"
    startup.writer.sendall.assert_not_called()
    startup.process.terminate.assert_called_once()
    assert not startup.directory.exists()


def test_readiness_probe_has_one_deadline_across_partial_reads(monkeypatch, tmp_path):
    from microcosm_emitter.host import client

    clock = SimpleNamespace(now=0.0)
    timeouts = []
    peer = Mock()
    peer.__enter__ = Mock(return_value=peer)
    peer.__exit__ = Mock(return_value=False)
    peer.settimeout.side_effect = timeouts.append

    def read(size):
        clock.now += 0.02
        return b"o"  # Never completes an acknowledgement.

    peer.recv.side_effect = read
    monkeypatch.setattr(client, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(client.socket, "socket", lambda *args: peer)
    with pytest.raises(LocalServiceError, match="TimeoutError"):
        client.SocketClient(tmp_path / "socket").ping(timeout=0.03)
    assert timeouts == pytest.approx([0.03, 0.03, 0.03, 0.01])
    assert peer.recv.call_count == 2


def test_unknown_reason_cannot_be_used_as_private_diagnostic():
    from microcosm_emitter.host.errors import ServiceStartupError

    with pytest.raises(ValueError, match="Unknown startup failure reason") as captured:
        ServiceStartupError("hf_synthetic_private_reason")
    assert "hf_synthetic_private_reason" not in str(captured.value)


def test_child_startup_stderr_redacts_exception_payload_and_spoofed_type(
    monkeypatch, tmp_path, capsys
):
    import io

    from microcosm_emitter.host import __main__ as entry

    directory = tmp_path / "runtime"
    directory.mkdir()
    monkeypatch.setattr(
        entry.sys,
        "argv",
        [
            "service",
            "--socket",
            str(directory / "socket"),
            "--parent-pid",
            "123",
            "--parent-created-at",
            "1",
        ],
    )
    monkeypatch.setattr(
        entry.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(
                b'{"test":{"token":"hf_synthetic_private_configuration"}}'
            )
        ),
    )
    private_error = type(
        "hf_synthetic_private_class", (Exception,), {"__module__": "builtins"}
    )
    monkeypatch.setattr(
        entry,
        "load_module",
        Mock(side_effect=private_error("Bearer hf_synthetic_private_payload")),
    )
    assert entry.main() == 1
    warning = capsys.readouterr().err
    assert "startup_failure" in warning
    assert "Exception" in warning
    assert "hf_synthetic_private" not in warning
    assert not directory.exists()
