"""The `microcosm-emitter` command installed with `uv tool install microcosm-emitter`.

`microcosm-emitter host` runs the local service host. A build starts it as its
own child process, so the host inherits the build's environment variables and
user, including any Hugging Face credential. It is never a system service.

`microcosm-emitter telemetry-token` runs on the operator's workstation when it
launches a build on a machine without a Hugging Face credential, such as
Modal: it obtains a collector token that can only write telemetry, for the
emitter on that machine. `microcosm-emitter status --spool` reports on a
spool database copied back from that machine without changing it, and
`microcosm-emitter deliver --spool` delivers it with this machine's login.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from enum import IntEnum, unique
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Final

from microcosm_emitter.compatibility import (
    SUPPORTED_PROTOCOL_VERSIONS,
    EmitterDescription,
    VersionReport,
)

if TYPE_CHECKING:
    from microcosm_emitter.core.auth import ExchangeFailureResult, SessionResult

DISTRIBUTION_NAME = "microcosm-emitter"


@unique
class ExitCode(IntEnum):
    """Every exit code of every `microcosm-emitter` command, in one table.

    ``unique`` refuses two members with one value when this module is
    imported, so a new code cannot take a meaning another command already
    gives it. Each comment names the commands that return the code.
    """

    #: Every command: it did what it was asked.
    SUCCESS = 0
    #: `host`: the host could not start. Python also exits 1 for an uncaught
    #: exception, so no other command uses it.
    HOST_START_FAILURE = 1
    #: Every command: a usage error, which argparse reports.
    USAGE_ERROR = 2
    #: `telemetry-token` and `deliver`: no Hugging Face credential; nothing
    #: was sent.
    MISSING_CREDENTIAL = 3
    #: `telemetry-token` and `deliver`: the collector refused the credential.
    REJECTED_CREDENTIAL = 4
    #: `telemetry-token` and `deliver`: the collector could not be reached or
    #: gave no usable token. `deliver`: it stayed unavailable until the time
    #: limit.
    COLLECTOR_UNAVAILABLE = 5
    #: `telemetry-token`: the collector does not accept `lifetime_seconds`.
    LIFETIME_UNSUPPORTED = 6
    #: `telemetry-token`: any error the other codes do not name; only the
    #: error's type is printed.
    UNEXPECTED_FAILURE = 7
    #: `deliver`: every other run was delivered, but the collector refused
    #: this login (403) for a run another Hugging Face user registered.
    RUN_OF_ANOTHER_USER = 8
    #: `status` and `deliver`: the file cannot be used as a spool database,
    #: because this user lacks permission or it is not one this emitter knows.
    UNREADABLE_SPOOL = 9


UNREADABLE_SPOOL_MESSAGE: Final = (
    "The spool database cannot be read ({error_type}): the file is not a spool "
    "database, or a newer emitter release wrote it."
)
SPOOL_PERMISSION_MESSAGE: Final = (
    "The spool database cannot be opened ({error_type}): permission denied. "
    "`status` needs to read the file; `deliver` also needs to write it and to "
    "create files in its directory."
)
#: Each failure to get a collector token, by the answer of
#: ``exchange_huggingface_credential`` (`telemetry-token`) or of the
#: ``CollectorSession`` that calls it (`deliver`): its exit code, and one line
#: for standard error, where ``{answer}`` is empty or the HTTP status. An
#: unsendable credential exits as a refused one, as the collector answered it
#: before the emitter checked it.
CREDENTIAL_FAILURES: Final[
    dict[ExchangeFailureResult | SessionResult, tuple[ExitCode, str]]
] = {
    "missing": (
        ExitCode.MISSING_CREDENTIAL,
        "No Hugging Face credential was found: set HF_TOKEN or HUGGINGFACE_TOKEN, "
        "or run `hf auth login`.",
    ),
    "unsendable": (
        ExitCode.REJECTED_CREDENTIAL,
        "The Hugging Face credential was not sent: it has characters that an HTTP "
        "header cannot carry, such as a space or a line break. Check HF_TOKEN, "
        "HUGGINGFACE_TOKEN or the login saved by `hf auth login`.",
    ),
    "rejected": (
        ExitCode.REJECTED_CREDENTIAL,
        "The collector refused the Hugging Face credential{answer}: it issues "
        "tokens only to members of the PolicyEngine organization.",
    ),
    "unavailable": (
        ExitCode.COLLECTOR_UNAVAILABLE,
        "The collector could not be reached or gave no usable token{answer}.",
    ),
    "lifetime_unsupported": (
        ExitCode.LIFETIME_UNSUPPORTED,
        "The collector refused the request{answer}: it does not accept "
        "`lifetime_seconds`, so it cannot issue a token for another machine yet.",
    ),
}
DELIVERY_UNFINISHED_MESSAGE: Final = (
    "Events are still pending after {seconds} seconds: the collector was "
    "unavailable. Run `deliver` again later; delivered events are not sent twice."
)
RUN_OF_ANOTHER_USER_MESSAGE: Final = (
    "Runs not delivered: {run_ids}. The collector refused this login (403): "
    "another Hugging Face user registered them, normally the operator who "
    "launched the build. They stay pending in the spool database; that "
    "operator should run `microcosm-emitter deliver --spool` on it."
)
#: After a login failure, before `deliver` opened the copy or partway.
DELIVERY_STOPPED_MESSAGE: Final = (
    "Runs not yet delivered stay pending in the spool database; run `deliver` "
    "again once that is resolved."
)
#: How long `deliver` keeps trying by default.
DEFAULT_DELIVERY_TIME_LIMIT_SECONDS: Final = 120
#: `huggingface_hub` could not read the login `hf auth login` saved, such as a
#: file this user may not read, and neither variable is set.
UNREADABLE_LOGIN_MESSAGE: Final = (
    "The Hugging Face login saved by `hf auth login` could not be read "
    "({error_type}), and neither HF_TOKEN nor HUGGINGFACE_TOKEN is set; nothing "
    "was sent."
)
#: Only the error's type: its text can quote the credential, as http.client's
#: error for a header it cannot send does.
TELEMETRY_TOKEN_UNEXPECTED_FAILURE: Final = (
    "No telemetry token was obtained: an unexpected {error_type} occurred. Its "
    "details are not printed, because they can contain the credential."
)


def describe() -> EmitterDescription:
    """Versions a client checks before sending any hand-over."""
    return {
        "emitter_version": version(DISTRIBUTION_NAME),
        "protocol_versions": list(SUPPORTED_PROTOCOL_VERSIONS),
    }


def version_report() -> VersionReport:
    """The versions, and the collector origin this emitter sends to by default."""
    from microcosm_emitter.core.collector_origin import resolve_collector_origin

    return {**describe(), "collector_origin": resolve_collector_origin()}


def _positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive whole number")
    return number


def _existing_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError("must name an existing file")
    return path


def _add_spool(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--spool",
        type=_existing_file,
        required=True,
        help="A spool database (events.sqlite3), such as one copied from a build "
        "on another machine.",
    )


def status(arguments: argparse.Namespace) -> int:
    """Print each run in a spool database as JSON, changing nothing."""
    from microcosm_emitter.telemetry.service.spool_copy import spool_status

    print(json.dumps({"runs": spool_status(arguments.spool)}))
    return ExitCode.SUCCESS


def deliver(arguments: argparse.Namespace) -> int:
    """Deliver a spool database's pending runs with this machine's login, then
    print its status as `status` does."""
    from microcosm_emitter.telemetry.service.spool_copy import (
        deliver_spool,
        spool_status,
    )

    delivery = deliver_spool(
        arguments.spool,
        development_collector_url=arguments.development_collector_url,
        time_limit_seconds=arguments.time_limit_seconds,
    )
    if delivery.login_failure is not None:
        exit_code, message = CREDENTIAL_FAILURES[delivery.login_failure]
        print(message.format(answer=""), file=sys.stderr)
        print(DELIVERY_STOPPED_MESSAGE, file=sys.stderr)
        return exit_code
    print(json.dumps({"runs": spool_status(arguments.spool)}))
    if delivery.runs_of_other_users:
        print(
            RUN_OF_ANOTHER_USER_MESSAGE.format(
                run_ids=", ".join(delivery.runs_of_other_users)
            ),
            file=sys.stderr,
        )
    if delivery.outcome == "time_limit_passed":
        print(
            DELIVERY_UNFINISHED_MESSAGE.format(seconds=arguments.time_limit_seconds),
            file=sys.stderr,
        )
        return ExitCode.COLLECTOR_UNAVAILABLE
    if delivery.runs_of_other_users:
        return ExitCode.RUN_OF_ANOTHER_USER
    return ExitCode.SUCCESS


def _spool_command(
    command: Callable[[argparse.Namespace], int], arguments: argparse.Namespace
) -> int:
    """Run `status` or `deliver`, exiting 9 for a file it cannot use."""
    from microcosm_emitter.telemetry.service.spool_copy import UnreadableSpoolError

    try:
        return command(arguments)
    except UnreadableSpoolError as error:
        message = (
            SPOOL_PERMISSION_MESSAGE
            if error.permission_denied
            else UNREADABLE_SPOOL_MESSAGE
        )
        print(message.format(error_type=error), file=sys.stderr)
        return ExitCode.UNREADABLE_SPOOL


def _collector_origin(development_collector_url: str | None) -> str:
    from microcosm_emitter.core.collector_origin import resolve_collector_origin

    try:
        return resolve_collector_origin(
            development_collector_url=development_collector_url
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def _development_collector_url(value: str) -> str:
    _collector_origin(value)
    return value


def _add_development_collector_url(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--development-collector-url",
        metavar="URL",
        type=_development_collector_url,
        help="A loopback collector, for tests and local development. Without "
        "it, the collector origin that `version` prints.",
    )


def telemetry_token(arguments: argparse.Namespace) -> int:
    """Print a telemetry token, its expiry and the collector origin as JSON.

    The token goes to standard output only. A failure prints one line on
    standard error, without the token, the Hugging Face credential or the
    collector's answer body; ``main`` turns any other error into exit 7.
    """
    from microcosm_emitter.core.auth import (
        ExchangeFailure,
        exchange_huggingface_credential,
        huggingface_token,
    )
    from microcosm_emitter.core.constants import TELEMETRY_SCOPE

    origin = _collector_origin(arguments.development_collector_url)
    try:
        credential = huggingface_token()
    except (OSError, UnicodeDecodeError) as error:
        print(
            UNREADABLE_LOGIN_MESSAGE.format(error_type=type(error).__name__),
            file=sys.stderr,
        )
        return ExitCode.MISSING_CREDENTIAL
    # Counted from before the request, so it never overstates the expiry.
    requested_at = time.time()
    exchange = exchange_huggingface_credential(
        origin,
        credential,
        scope=TELEMETRY_SCOPE,
        lifetime_seconds=arguments.lifetime_seconds,
    )
    if isinstance(exchange, ExchangeFailure):
        exit_code, message = CREDENTIAL_FAILURES[exchange.result]
        answer = (
            " (no answer)" if exchange.status is None else f" (HTTP {exchange.status})"
        )
        print(message.format(answer=answer), file=sys.stderr)
        return exit_code
    expires_at = datetime.fromtimestamp(requested_at + exchange.expires_in, UTC)
    print(
        json.dumps(
            {
                "access_token": exchange.access_token,
                "expires_at": expires_at.isoformat(timespec="seconds"),
                "collector_origin": origin,
            }
        )
    )
    return ExitCode.SUCCESS


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    parser = argparse.ArgumentParser(prog="microcosm-emitter", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "host",
        help="Run the local service host; its configuration arrives on stdin.",
        add_help=False,
    )
    commands.add_parser(
        "version",
        help="Print the emitter and socket protocol versions and the collector "
        "origin as JSON.",
    )
    token_parser = commands.add_parser(
        "telemetry-token",
        help="Exchange this machine's Hugging Face login for a collector token "
        "that only writes telemetry, for a build on a machine without one. "
        "Prints the token, its expiry and the collector origin as JSON.",
    )
    token_parser.add_argument(
        "--lifetime-seconds",
        type=_positive_integer,
        required=True,
        help="How long the token should last; the collector may grant less.",
    )
    _add_development_collector_url(token_parser)
    status_parser = commands.add_parser(
        "status",
        help="Print each run in a spool database, with its upload state and "
        "undelivered events, as JSON. Reads only: no network, no changes.",
    )
    _add_spool(status_parser)
    deliver_parser = commands.add_parser(
        "deliver",
        help="Deliver a spool database's pending runs, such as a copy from a "
        "build on another machine, with this machine's Hugging Face login, then "
        "print its status as JSON. Never prunes undelivered events or changes "
        "permissions; a run another Hugging Face user registered stays pending.",
    )
    _add_spool(deliver_parser)
    deliver_parser.add_argument(
        "--time-limit-seconds",
        type=_positive_integer,
        default=DEFAULT_DELIVERY_TIME_LIMIT_SECONDS,
        help="How long to keep trying while the collector is unavailable.",
    )
    _add_development_collector_url(deliver_parser)
    if arguments[:1] == ["host"]:
        # The host parses its own options.
        from microcosm_emitter.host.__main__ import main as host_main

        return host_main(arguments[1:], description=describe())
    parsed = parser.parse_args(arguments)
    if parsed.command == "telemetry-token":
        try:
            return telemetry_token(parsed)
        except Exception as error:
            print(
                TELEMETRY_TOKEN_UNEXPECTED_FAILURE.format(
                    error_type=type(error).__name__
                ),
                file=sys.stderr,
            )
            return ExitCode.UNEXPECTED_FAILURE
    if parsed.command == "status":
        return _spool_command(status, parsed)
    if parsed.command == "deliver":
        return _spool_command(deliver, parsed)
    print(json.dumps(version_report()))
    return ExitCode.SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
