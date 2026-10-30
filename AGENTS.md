# Development instructions

The Rules section below is generated from `rules/registry.toml`. Change a rule
in the registry, never in this file.

## Rules

<!-- BEGIN GENERATED: rules (python -m scripts.rules render) -->

Every rule for this repository lives in `rules/registry.toml` and is rendered,
with its enforcement and gaps, in `docs/generated/rules.md`. Blocking rules
fail CI. Doc-only rules are not checked automatically, so follow them yourself.
To add or change a rule, read `docs/engineering/skills/rules-registry.md`.

### Runtime and package boundaries

- `emitter.boundaries.graph-adapter-imports-no-service-code`: Graph adapters, the Orrery client and its contracts, import no service, database, authentication or HTTP code: inventory hashing, durable enqueue, receipt lookup and retries run in the separate local emitter service, and the client never falls back to writing the database directly. (blocking: pytest `tests/orrery/test_client.py`, `tests/test_boundaries.py`)
- `emitter.boundaries.host-imports-no-domain-code`: The generic runtime, microcosm_emitter.host, imports no telemetry, Orrery, database or authentication code, no Microcosm, SQLAlchemy, Alembic or Hugging Face Hub module, and not the interface package. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.boundaries.interface-imports-only-the-standard-library`: The interface package imports only the standard library and itself, and importing it loads no microcosm_emitter module. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.boundaries.public-credential-lookup`: Code reads the Hugging Face credential through core.auth.huggingface_token; no module imports the private _huggingface_token. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.boundaries.shared-definitions-written-once`: What both packages use is defined once: the emitter's in-process clients reuse the interface's TelemetryRun, wire_value, Orrery publication types and default_spool_path rather than copies, and each hand-over failure code and limit the packages share is assigned at module level exactly once across both. (blocking: pytest `tests/interface/test_in_process_clients.py`, `tests/test_boundaries.py`)
- `emitter.boundaries.telemetry-client-imports-no-service-code`: The telemetry client, microcosm_emitter.telemetry outside its service package, imports no service implementation, database or authentication module, and importing the public modules opens no socket, starts no process and creates no directory. (blocking: pytest `tests/test_boundaries.py`)

### Databases and migrations

- `emitter.persistence.sqlalchemy-and-alembic-only`: Python databases use the SQLAlchemy declarative ORM and Alembic only: no handwritten SQL, no direct-driver persistence, no create_all(), no schema repair and no adoption of databases made before Alembic. (blocking: pytest `tests/test_boundaries.py`)
- `emitter.persistence.typescript-uses-drizzle`: TypeScript database code, if any is added, uses Drizzle ORM and Drizzle Kit migrations. (doc-only, not checked: follow it yourself)

### Distributions and releases

- `emitter.distributions.artifacts-verified`: Every pull request builds both distributions, each wheel from its source archive, passes twine check --strict on them, and has tools/check-installed.py require one wheel and one source archive per distribution, refuse a source archive holding the rules tool, and run the installed smoke checks (tools/installed-smoke.py and tools/installed-interface-smoke.py) in clean environments outside the checkout. (blocking: CI job `build-packages`, `tests`)
- `emitter.distributions.exactly-two-at-one-version`: The repository publishes exactly two distributions, always together and at one version: microcosm-emitter, which installs the generic host and its required telemetry with no extras, and the dependency-free microcosm-emitter-interface, which microcosm-emitter==X requires at ==X; there are no standalone telemetry packages, optional telemetry installs or further distributions. (blocking: pytest `tests/test_package_names.py`)
- `emitter.distributions.one-build-script`: Only tools/check-artifacts.sh builds the distributions, with one uv build --all-packages into a dist directory it removes beforehand and no --clear, no workflow runs uv build itself, and tools/check-installed.py checks the interface wheel apart from the emitter. (blocking: pytest `tests/test_workflows.py`)
- `emitter.distributions.release-credentials`: In push.yaml only the publish job, in the pypi environment, may request an OpenID Connect token, and the workflow reads no secrets but APP_ID and APP_PRIVATE_KEY, once each, for the GitHub App token that commits the version. (blocking: pytest `tests/test_workflows.py`)
- `emitter.distributions.release-order`: push.yaml releases a merge to main in order (the version commit, then ci.yml on that commit, then the tag, then publication) and publishes to PyPI only the artifacts ci.yml built, one per distribution, the interface first, skipping files PyPI already holds so a rerun completes a partial upload. (blocking: pytest `tests/test_workflows.py`)
- `emitter.distributions.source-archives-exclude-the-rules-tool`: The microcosm-emitter source archive leaves out the rules tool, the registry and their documents, and tools/check-installed.py refuses either source archive if it holds any of them, naming the same paths the build excludes. (blocking: pytest `tests/test_source_archives.py`)
- `emitter.distributions.version-from-fragments`: A release's version bump follows its changelog fragment types (breaking is a major bump, added or removed a minor bump, anything else a patch bump) and rewrites both distributions' versions and the interface pin together, and the release refuses diverged versions, a different pin, missing or untyped fragments, and a missing or repeated version line. (blocking: pytest `tests/test_workflows.py`)

### Collector protocol

- `emitter.protocol.collector-contract`: The installed wheels deliver telemetry to the collector at the commit tools/check-collector.sh pins, with a disposable PostgreSQL database: events become dashboard documents, rejected identities register nothing, a lost acknowledgement is retried without duplicates, and blocked outcomes and supplied failure codes survive. (blocking: pytest `tests/test_collector_contract.py`)
- `emitter.protocol.every-fixture-sent-and-answered`: Every protocol fixture has a test in which the interface sends it, and this emitter answers each one: the telemetry, Orrery and Hugging Face modules handle their message and query fixtures, the installed command starts from the launch fixture and answers hello with at least the recorded keys, and the host routes each envelope fixture to its module. (blocking: pytest `tests/interface/test_host_protocol.py`, `tests/interface/test_in_process_clients.py`, `tests/interface/test_protocol_fixtures.py`)
- `emitter.protocol.no-hosted-events-api`: Preserve the existing collector protocol, and do not add a general hosted events API. (doc-only, not checked: follow it yourself)
- `emitter.protocol.released-fixtures-unchanged`: A released protocol fixture is never edited or removed, since each one's SHA-256 is pinned, and every fixture's protocol version is one this emitter accepts. (blocking: pytest `tests/interface/test_protocol_fixtures.py`)
- `emitter.protocol.trusted-collector-origins`: Every trusted collector origin is a bare HTTPS origin that a module configuration selects as itself, with or without a trailing slash. (blocking: pytest `tests/test_collector_origin.py`)

### Test isolation

- `emitter.tests.no-ambient-credentials-or-hosted-collector`: Tests never use ambient credentials or contact a hosted collector: an autouse fixture in conftest.py removes the Hugging Face credential variables, gives each test a temporary Hugging Face home, and fails a test whose telemetry or authentication request names a host other than loopback. (blocking: CI job `tests`)
- `emitter.tests.real-migrations-synthetic-credentials`: Tests use temporary databases created through real migrations, synthetic credentials and loopback network services. (doc-only, not checked: follow it yourself)

### Code quality

- `emitter.quality.ruff-format`: All Python is formatted with ruff format. (blocking: ruff format)
- `emitter.quality.ruff-lint`: All Python passes ruff check with the rule families E, F, I, N, W, UP and B selected, except E501 (line length), which ruff format settles. (blocking: ruff check (B, E, F, I, N, UP, W))
- `emitter.quality.tests-pass`: Every test except the collector contract test passes on Ubuntu and macOS with Python 3.13 and 3.14, in the environment uv sync --locked installs. (blocking: CI job `tests`)
- `emitter.quality.toolchain`: Use uv, Python 3.13 or later, pytest and Ruff. (doc-only, not checked: follow it yourself)

### CI workflows and scripts

- `emitter.ci.multiline-shell-in-scripts`: No workflow run: step spans more than one line; shell logic of several lines lives in a Bash script under tools/, which the step calls with a single command. (blocking: pytest `tests/test_workflows.py`)
- `emitter.ci.pull-request-workflow-structure`: pr.yaml runs on pull requests and calls ci.yml, the changelog check and the rules registry, and its Required checks job runs always and needs each other job; ci.yml is called only by other workflows and has exactly the quality, tests, build-packages and collector jobs, testing on Ubuntu and macOS with Python 3.13 and 3.14, with the collector job after build-packages. (blocking: pytest `tests/test_workflows.py`)
- `emitter.ci.required-checks`: Every pull request runs the verification workflow, ci.yml, and Required checks, the one status check main's ruleset requires, passes only when every other pull-request job succeeded. (blocking: CI job `verify`, `required`)
- `emitter.ci.shell-scripts-parse`: Every shell script under tools/ parses (bash -n). (blocking: CI job `quality`)
- `emitter.ci.workflows-pass-actionlint`: Every workflow passes actionlint 1.7.12, which also runs shellcheck on each run: step. (blocking: CI job `lint-workflows`)

### Process and repository hygiene

- `emitter.agents.adapters-point-to-agents-md`: CLAUDE.md only points to AGENTS.md; guidance and rules are written in AGENTS.md alone. (blocking: quality guard `rules-registry`)
- `emitter.process.changelog-fragment`: Every pull request adds at least one changelog fragment, changelog.d/<name>.<type>.md with a type of breaking, added, changed, fixed or removed, and keeps both distributions' versions and the interface pin consistent, so its merge can be released. (blocking: CI job `changelog`)
- `emitter.process.feature-branches-and-pull-requests`: Push feature branches and open pull requests; never push to main directly, merge automatically or monitor queued checks. (doc-only, not checked: follow it yourself)
- `emitter.process.rules-registry`: Every rule is registered in rules/registry.toml, is enforced where it claims to be, and its rendered views are current. (blocking: quality guard `rules-registry`)

<!-- END GENERATED: rules -->
