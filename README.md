# Microcosm emitter

`microcosm-emitter` is one Python distribution containing the process host, required telemetry library and completed-graph publication library:

- `microcosm_emitter.host` owns subprocess startup, a private Unix socket, explicit module loading, parent-process monitoring, and shutdown. Its code does not import telemetry, database, authentication, or Microcosm modules.
- `microcosm_emitter.telemetry.client` is the thin build adapter. It starts the local telemetry emitter service and sends build requests, without importing the service implementation, database, or authentication libraries.
- `microcosm_emitter.telemetry.service` interprets those requests in the separate process. It owns stage tracking, event construction, credential redaction, resource sampling, SQLAlchemy ORM persistence, Alembic migrations, authentication, and collector delivery.
- `microcosm_emitter.orrery.client` sends graph inventory, enqueue, receipt and retry requests over the same private socket. It does not hash files, open a database, authenticate or upload.
- `microcosm_emitter.orrery.service` validates and hashes the selected graph files, commits publication jobs, and uploads exact bytes with retry and expiring worker reservations. A graph publication requires no telemetry run registration.
- `microcosm_emitter.core` provides service-side authentication and SQLAlchemy/Alembic persistence shared by the telemetry and graph libraries. The generic host does not import it.

Installing `microcosm-emitter` always installs the telemetry library and all its dependencies. Telemetry is not a standalone distribution or an optional extra. The package keeps import boundaries between the host, adapter, and service implementation; they do not need separate installation or publication.

The build process imports only the adapter, not the service implementation. It supplies run metadata, its existing queue path, and build identity. The adapter starts a separate local telemetry emitter service process automatically and sends requests over a private Unix socket. The adapter does not track stages, construct events, or redact telemetry. Imports alone create no processes, sockets, files, or network requests.

## Delivery and persistence

The service interprets requests such as a stage change or build failure, timestamps and redacts the resulting events, and saves them before acknowledging the socket request. A stage change can complete one stage and start another; both events commit in one SQLAlchemy ORM transaction, and the service changes its stage state only after that transaction succeeds. Periodic retention maintenance runs outside this acknowledgement path.

The adapter bounds socket calls and reports telemetry failures without failing the build. It converts Python arguments into JSON but does not process their telemetry meaning. Network requests run on the service worker, outside the message-handling lock.

`block()` finishes the current stage and records that validation checks refused the build, including the phase, check identifiers, failure count and optional check statuses. `fail(error_code=...)` preserves the caller's failure code alongside the exception type and failure class. Microcosm classifies interruptions, process termination and memory failures; this package forwards those classifications rather than duplicating them. Both outcomes finish telemetry, so later heartbeat or parent-exit handling cannot replace them.

The service uses the existing Hugging Face credential from the environment or local login cache. The collector validates PolicyEngine membership before issuing a short-lived write credential. Missing or rejected credentials permanently mark that producer's events as local-only. Permanent HTTP client errors when registering a run or uploading events also keep only the affected producer's records local; HTTP 408, 429, server errors and network failures remain retryable. HTTP 401 invalidates the short-lived credential for renewal. The production destination belongs to the package; development overrides accept loopback addresses only.

The queue upgrades the existing `20261007_01` Alembic revision to `20261008_02`, adding graph-job records without changing producer sequences, event IDs or upload eligibility. Unversioned databases are rejected without schema modification. There is no raw SQL, automatic schema adoption, or parallel legacy implementation. The hosted telemetry collector and calibration dashboard do not change.

## Graph publication

`GraphPublicationClient.start()` starts the graph module in a separate local emitter service process. `inventory()` asks that process to hash an explicit set of allowed graph files; `enqueue()` returns a pending receipt only after the job commits. `receipt()` reads its result and `retry()` makes a pending job eligible again. Startup or socket failures return an unavailable result, not a claim that a job was saved; the build adapter never writes a fallback database.

Graph requests allow up to 30 seconds for bounded file validation and hashing; telemetry acknowledgements, readiness probes and close requests keep their existing short timeout. Very slow storage can still exceed the graph request limit, so a timeout is not proof that a late service-side commit did or did not occur. Repeating an enqueue with the same publication ID and inventory is idempotent.

The host can also start telemetry and graph modules together with `start_services()`. Each module has an independent worker, so a slow graph upload does not block telemetry requests. Construct the telemetry adapter with `close_service_on_finish=False` when a build must publish graphs after recording completion, failure or refusal. Telemetry still finishes, but explicit outer cleanup closes the shared host after graph requests; standalone adapters close automatically by default. A module-specific graph client does not own shared-host shutdown. Both libraries share an existing HF credential and short-lived collector session, but graph jobs remain pending independently of telemetry event retention or local-only upload eligibility.

Preserve the selected source files until publication succeeds. `microcosm-emitter-publish-graph` asks the service to retry a publication by ID and can re-enqueue a preserved directory on another machine. It never rewrites the original build receipt; the queue and separate latest-status file record retries.

## Development

Use Python 3.13 or 3.14 on Linux or macOS.

```bash
uv sync --locked
bash tools/check-quality.sh
uv run --no-sync pytest -m 'not collector'
bash tools/check-artifacts.sh
bash tools/check-changelog.sh
```

Tests isolate ambient credentials and use synthetic identities. The artifact check builds one source archive and wheel, installs that wheel into a clean environment away from the source checkout, verifies import boundaries, and exercises telemetry-only, graph-only and combined service startup with packaged migrations.

The collector compatibility job tests the installed wheel against a pinned dashboard collector and disposable PostgreSQL. It covers the HTTP exchange, event ingestion, blocked outcomes and supplied failure codes in dashboard run documents, rejected identities, and duplicate delivery after a lost acknowledgement. Local execution requires that pinned collector checkout and a loopback test database; `tools/check-collector.sh` verifies these inputs. Never point this test at a deployed database. The pin verifies source compatibility, not which revision the hosted collector currently runs.

## Publication

Every merge to `main` releases the Python distribution `microcosm-emitter` to PyPI. Each pull request adds a changelog fragment named `changelog.d/<name>.<type>.md`, where the type is `breaking`, `added`, `changed`, `fixed` or `removed`; `tools/check-changelog.sh` refuses a pull request without one.

`push.yaml` runs twice for each merge:

1. On the merge commit, it bumps the version in `pyproject.toml` and `uv.lock`: a major bump for a breaking fragment, a minor bump for an added or removed fragment, and a patch bump otherwise. It collects the fragments into `CHANGELOG.md` and commits the result as "Update microcosm-emitter" using the PolicyEngine GitHub App, which may bypass the `main` ruleset.
2. On that commit, it runs every CI check, tags the commit with the bare version (for example `0.2.0`), and publishes the tested wheel and source archive to PyPI. Each step starts only after the one before succeeds.

PyPI accepts uploads only through trusted publishing from `push.yaml` in the `pypi` environment, which only `main` can use. CI holds no long-lived registry token. Renaming the workflow or the environment stops publication until PyPI's trusted-publisher settings change to match.

No hosted-service deployment or consumer activation follows from publishing this package.
