## [0.6.0] - 2026-10-21

### Added

- Add `stage_dataset_bundle`, `dataset_bundle_receipt` and `check_write_access`. A build hands over its staged dataset bundle with each file's role; the emitter writes it under `staged/<run_id>/` in one commit with Microcosm's message, paths and idempotency (`already_staged`, `REMOTE_DIFFERS`), and retries Hub failures from the durable queue. The emitter copies the bundle's small files (each up to 32 MiB, up to 64 MiB per hand-over) into its queue before answering, so the build may rewrite them at once, as Microcosm does with its build manifest, checksum listing and build record; it uploads larger files from their paths and never uploads one whose bytes changed after the hand-over. Bundles upload on a worker thread of their own, so a long upload never delays run-file commits to another repository, the commit at shutdown, or a read-back.
- Add a durable Hugging Face hand-over queue in its own SQLite database next to the spool database (`events.handovers.sqlite3`, with its own Alembic history), so the shared spool database stays at a revision that released emitters accept; `HuggingFaceSettings.handover_database_path` can name another file. Staging run files keep only the newest handed-over bytes per repository path; dataset bundles and releases are jobs with leases, retries and receipts; and a per-repository schedule spaces commits and lets one writer on a machine hold a repository at a time. Hugging Face calls use the build's own credential, found in the same order as telemetry's.
- Add the `huggingface` emitter module and the interface's `stage_run_files`, `run_files_receipt` and `read_back_run_files` calls. A build hands over its staging run files; the emitter writes them at the same paths with the same bytes as Microcosm's staging writers (`runs/<run_id>/...`, and for staging contract version 1 `latest_staging.json` and a merged `runs.json`), in one Hugging Face commit per repository per cycle instead of one commit per file. Uploads stay best-effort and are retried from the durable queue.


## [0.5.1] - 2026-10-21

### Fixed

- - Lock certifi 2026.10.19, which removes the "emSign ECC Root CA - G3" root. The previously locked certifi 2026.7.22 trusted eMudhra intermediate CA certificates whose private keys can be recovered from their public certificates (https://emudhra.we-have-a-problem.com), so a development or CI environment synced from the lockfile could accept forged certificates for the collector or Orrery publication hosts. Only the lockfile changes: `uv tool install microcosm-emitter` resolves its own dependency versions.


## [0.5.0] - 2026-10-21

### Added

- Add the `microcosm-emitter-interface` distribution: a dependency-free package that a Microcosm build depends on with a version range. `EmitterClient.start` runs the installed `microcosm-emitter` command as the build's child process, which inherits the build's environment and finds the same Hugging Face credential, and refuses an emitter that does not accept its socket protocol version. It offers typed calls for telemetry events and Orrery input hand-overs, and never raises into the build: not because the emitter is missing or refusing a request, and not because an argument cannot be converted to JSON. A refused call reports why in `EmitterClient.last_failure`. The emitter's in-process telemetry and Orrery clients now build their messages with the interface's code, and the in-process Orrery client reports an argument it cannot send (`invalid_arguments`) instead of raising.


## [0.4.0] - 2026-10-20

### Added

- Add the `microcosm-emitter` command, so `uv tool install microcosm-emitter` installs the emitter outside the build's Python environment. A build starts `microcosm-emitter host` as its own child process, which inherits the build's environment and user. The host can read its parent's creation time itself, and can skip requested modules that it does not install (it still refuses to start if none is installed). Only a host started by `microcosm-emitter host` answers `hello`, with the emitter version, accepted socket protocol versions and loaded and skipped modules; a host started with `python -m microcosm_emitter.host` refuses it.


## [0.3.0] - 2026-10-14

### Removed

- Remove the unused TypeScript reader `@policyengine/microcosm-provider-orrery`, so each release now publishes only the `microcosm-emitter` Python distribution to PyPI.


## [0.2.1] - 2026-10-14

### Fixed

- Publish the reader tarball by its relative path. npm treated the bare reader-dist path as a GitHub repository and never published the reader for 0.2.0.


## [0.2.0] - 2026-10-14

### Added

- Extract the local-service host, thin build adapter, and required telemetry library into the `microcosm-emitter` distribution.
- Add independent completed-graph publication and verified Orrery document readers alongside telemetry in the local provider.

### Changed

- Release automatically on every merge to main: bump one shared version from the changelog fragment types, commit the changelog, run all checks, tag the commit with the bare version, then publish to PyPI and npm through trusted publishing. Pull requests now require a typed changelog fragment, and GitHub releases no longer trigger publication.
- Define explicit types for publication inventories and results, and return a frozen record when reserving a graph upload.
- Integrate completed-graph publication into the single microcosm-emitter package. Keep graph adapters limited to socket requests and preserve shared SQLAlchemy/Alembic persistence, telemetry lifecycle behavior and separate TypeScript readers.
- Consolidate the process host and required telemetry library into one `microcosm-emitter` distribution, including Python imports, installed-module registration, CI, artifact verification, and publication.

  Rename the GitHub project to `PolicyEngine/microcosm-emitter` and update source metadata, installation checks, and publishing documentation.

  Rename the CI package-build job to `build-packages` and update its dependent jobs.

### Fixed

- Use a shared 15-second startup default, enforce configuration and readiness deadlines, and report safe startup failure reasons without retaining private exception details.
- Keep graph file requests independent of telemetry acknowledgements, drain accepted requests before closing modules, and prevent older attempts from replacing newer graph publication status documents.
- Preserve valid stage identifiers unchanged in telemetry events and heartbeats. Reject malformed stage identifiers before persistence while continuing to redact credentials from messages and details.
- Preserve validation-refused build outcomes and caller-supplied failure codes in the telemetry service. Allow shared-host cleanup to wait until graph publication finishes. Retain only rejected producers' records locally after permanent collector HTTP client errors, and test these behaviors against the installed package and collector.
- Keep graph publication running when a preserved directory is removed or the local queue reports an error, and record the latest status only after the queue commit.
- Move stage tracking, event construction, and credential redaction into the separate local telemetry emitter service. Keep the build adapter limited to process startup, request serialization, socket transport, and shutdown. Commit all events from each request atomically before acknowledging it.

  Wait for a successful socket ping before runtime tests send requests, and clean up the service when test setup fails. Cover delayed listening and failed initialization with regression tests.
