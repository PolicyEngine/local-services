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
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import Final

from microcosm_emitter.compatibility import (
    SUPPORTED_PROTOCOL_VERSIONS,
    EmitterDescription,
    VersionReport,
)

DISTRIBUTION_NAME = "microcosm-emitter"

#: Exit codes of `telemetry-token` and `deliver`, one per kind of failure; 2
#: is a usage error, as for every command.
EXIT_MISSING_CREDENTIAL: Final = 3
EXIT_REJECTED_CREDENTIAL: Final = 4
EXIT_COLLECTOR_UNAVAILABLE: Final = 5
EXIT_LIFETIME_UNSUPPORTED: Final = 6
#: `status --spool` and `deliver --spool`: the file cannot be used as a spool
#: database, because this user lacks permission or it is not one this
#: emitter knows.
EXIT_UNREADABLE_SPOOL: Final = 7
#: `deliver --spool`: every other run was delivered, but the collector refused
#: this login (403) for a run another Hugging Face user registered.
EXIT_RUN_OF_ANOTHER_USER: Final = 8
UNREADABLE_SPOOL_MESSAGE: Final = (
    "The spool database cannot be read ({error_type}): the file is not a spool "
    "database, or a newer emitter release wrote it."
)
SPOOL_PERMISSION_MESSAGE: Final = (
    "The spool database cannot be opened ({error_type}): permission denied. "
    "`status` needs to read the file; `deliver` also needs to write it and to "
    "create files in its directory."
)
#: Each failure to get a collector token: its exit code, and one line for
#: standard error, where ``{answer}`` is empty or the HTTP status.
CREDENTIAL_FAILURES: Final = {
    "missing": (
        EXIT_MISSING_CREDENTIAL,
        "No Hugging Face credential was found: set HF_TOKEN or HUGGINGFACE_TOKEN, "
        "or run `hf auth login`.",
    ),
    "rejected": (
        EXIT_REJECTED_CREDENTIAL,
        "The collector refused the Hugging Face credential{answer}: it issues "
        "tokens only to members of the PolicyEngine organization.",
    ),
    "unavailable": (
        EXIT_COLLECTOR_UNAVAILABLE,
        "The collector could not be reached or gave no usable token{answer}.",
    ),
    "lifetime_unsupported": (
        EXIT_LIFETIME_UNSUPPORTED,
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
#: How long `deliver` keeps trying by default.
DEFAULT_DELIVERY_TIME_LIMIT_SECONDS: Final = 120


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
    return 0


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
    match delivery.outcome:
        case "missing" | "rejected" | "unavailable" as failure:
            exit_code, message = CREDENTIAL_FAILURES[failure]
            print(message.format(answer=""), file=sys.stderr)
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
        return EXIT_COLLECTOR_UNAVAILABLE
    if delivery.runs_of_other_users:
        return EXIT_RUN_OF_ANOTHER_USER
    return 0


def _spool_command(
    command: Callable[[argparse.Namespace], int], arguments: argparse.Namespace
) -> int:
    """Run `status` or `deliver`, exiting 7 for a file it cannot use."""
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
        return EXIT_UNREADABLE_SPOOL


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

    The token goes to standard output only; a failure prints one line on
    standard error, without the token or the collector's answer body.
    """
    from microcosm_emitter.core.auth import exchange_telemetry_token

    origin = _collector_origin(arguments.development_collector_url)
    exchange = exchange_telemetry_token(origin, arguments.lifetime_seconds)
    if exchange.result != "ok":
        exit_code, message = CREDENTIAL_FAILURES[exchange.result]
        answer = (
            " (no answer)" if exchange.status is None else f" (HTTP {exchange.status})"
        )
        print(message.format(answer=answer), file=sys.stderr)
        return exit_code
    print(
        json.dumps(
            {
                "access_token": exchange.access_token,
                "expires_at": exchange.expires_at,
                "collector_origin": origin,
            }
        )
    )
    return 0


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
        return telemetry_token(parsed)
    if parsed.command == "status":
        return _spool_command(status, parsed)
    if parsed.command == "deliver":
        return _spool_command(deliver, parsed)
    print(json.dumps(version_report()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
