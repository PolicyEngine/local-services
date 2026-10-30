# Development instructions

- Use uv, Python 3.13+, pytest, and Ruff. Keep workflows as single-command calls to Bash scripts when shell logic has multiple lines.
- The generic runtime must not import telemetry or Microcosm. The telemetry client must not import service implementation, database, or authentication modules.
- Graph adapters must not import service, database, authentication or HTTP code. Inventory hashing, durable enqueue, receipt lookup and retries run in the separate local emitter service; no direct-database fallback.
- Publish exactly two distributions, always together and at the same version: `microcosm-emitter` (the separately installed emitter, with its generic host and required telemetry) and `microcosm-emitter-interface` (the dependency-free package Microcosm builds import to start and call the emitter). `microcosm-emitter==X` requires `microcosm-emitter-interface==X`. Keep the generic host and required telemetry library internally separate under `microcosm_emitter`; do not introduce standalone telemetry packages, optional telemetry installation, or further distributions.
- Python databases use SQLAlchemy declarative ORM and Alembic exclusively. No handwritten SQL, direct-driver persistence, create_all(), schema repair, or pre-Alembic adoption. TypeScript database work, if introduced, must use Drizzle ORM and Drizzle Kit migrations.
- Tests use temporary databases created through real migrations, synthetic credentials, and loopback network services. Never use ambient credentials or contact the production collector in tests.
- Preserve the existing collector protocol; do not add a general hosted events API.
- Push feature branches and open PRs; do not push directly to main. Do not automatically merge or monitor queued checks.

The rules above are not registered in `rules/registry.toml` yet, so the Rules section below does not list them and the registry guard does not check them. Follow them as written; they are being moved into the registry.

## Rules

<!-- BEGIN GENERATED: rules (python -m scripts.rules render) -->

The rules registered in `rules/registry.toml` are listed below, and
rendered with their enforcement and gaps in `docs/generated/rules.md`.
Blocking rules fail CI. Doc-only rules are not checked automatically, so
follow them yourself. To add or change a rule, read `docs/engineering/skills/rules-registry.md`.

### Code quality

- `emitter.quality.ruff-lint`: All Python passes ruff check with the rule families E, F, I, N, W, UP and B selected, except E501 (line length), which ruff format settles. (blocking: ruff check (B, E, F, I, N, UP, W))

### Process and repository hygiene

- `emitter.agents.adapters-point-to-agents-md`: CLAUDE.md only points to AGENTS.md; guidance and rules are written in AGENTS.md alone. (blocking: quality guard `rules-registry`)
- `emitter.process.rules-registry`: Every rule is registered in rules/registry.toml, is enforced where it claims to be, and its rendered views are current. (blocking: quality guard `rules-registry`)

<!-- END GENERATED: rules -->
