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

The service uses the existing Hugging Face credential from the environment or local login cache. The collector validates PolicyEngine membership before issuing a short-lived write credential. Missing or rejected credentials permanently mark that producer's events as local-only. Transient collector failures retain events for retry. The production destination belongs to the package; development overrides accept loopback addresses only.

The queue upgrades the existing `20261007_01` Alembic revision to `20261008_02`, adding graph-job records without changing producer sequences, event IDs or upload eligibility. Unversioned databases are rejected without schema modification. There is no raw SQL, automatic schema adoption, or parallel legacy implementation. The hosted telemetry collector and calibration dashboard do not change.

## Graph publication

`GraphPublicationClient.start()` starts the graph module in a separate local emitter service process. `inventory()` asks that process to hash an explicit set of allowed graph files; `enqueue()` returns a pending receipt only after the job commits. `receipt()` reads its result and `retry()` makes a pending job eligible again. Startup or socket failures return an unavailable result, not a claim that a job was saved; the build adapter never writes a fallback database.

Graph requests allow up to 30 seconds for bounded file validation and hashing; telemetry acknowledgements, readiness probes and close requests keep their existing short timeout. Very slow storage can still exceed the graph request limit, so a timeout is not proof that a late service-side commit did or did not occur. Repeating an enqueue with the same publication ID and inventory is idempotent.

The host can also start telemetry and graph modules together with `start_services()`. Each module has an independent worker, so a slow graph upload does not block telemetry requests. Completing telemetry does not stop the graph module, and a module-specific client does not own shared-host shutdown. Both libraries share an existing HF credential and short-lived collector session, but graph jobs remain pending independently of telemetry event retention or local-only upload eligibility.

Preserve the selected source files until publication succeeds. `microcosm-emitter-publish-graph` asks the service to retry a publication by ID and can re-enqueue a preserved directory on another machine. It never rewrites the original build receipt; the queue and separate latest-status file record retries.

The existing TypeScript package `@policyengine/microcosm-provider-orrery` reads immutable published graph files on a server or in a browser. It remains separate from the Python distribution and does not change the hosted telemetry API.

## Development

Use Python 3.13 or 3.14 on Linux or macOS.

```bash
uv sync --locked
bash tools/check-quality.sh
uv run --no-sync pytest -m 'not collector'
bash tools/check-artifacts.sh
bash tools/check-reader.sh
```

Tests isolate ambient credentials and use synthetic identities. The artifact check builds one source archive and wheel, installs that wheel into a clean environment away from the source checkout, verifies import boundaries, and exercises telemetry-only, graph-only and combined service startup with packaged migrations. Reader checks cover tests, formatting, types, build and an isolated tarball installation; they do not inspect visual styling.

The collector compatibility job tests the installed wheel against a pinned dashboard collector and disposable PostgreSQL. It covers the HTTP exchange, event ingestion, dashboard run documents, rejected identities, and duplicate delivery after a lost acknowledgement. Local execution requires that pinned collector checkout and a loopback test database; `tools/check-collector.sh` verifies these inputs. Never point this test at a deployed database.

## Publication

The project publishes one Python distribution, `microcosm-emitter`. A published GitHub release triggers version and main-branch ancestry checks, all CI checks, and artifact verification. Only then can the protected `pypi` environment approve publishing the exact tested wheel and source archive. The publish job uses PyPI Trusted Publishing; no long-lived publishing token is supplied through CI. The separate TypeScript reader publication waits for Python publication and publishes the verified tarball through npm OIDC.

First publication requires one PyPI pending trusted publisher for `microcosm-emitter`, attached to GitHub owner `PolicyEngine`, project `microcosm-emitter`, workflow `publish.yml`, and environment `pypi`. That external setup must be verified before releasing. No release is created or merged automatically by this project.

The npm reader requires registry-owner first-publication and trusted-publisher setup; this code does not provision or verify those private account settings. No hosted-service deployment, registry publication or consumer activation follows from installing this package.
