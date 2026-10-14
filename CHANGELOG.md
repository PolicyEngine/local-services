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
