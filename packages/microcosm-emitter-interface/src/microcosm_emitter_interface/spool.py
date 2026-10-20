"""Where the emitter keeps its durable queues."""

import os
from pathlib import Path


def default_spool_path() -> Path:
    """The emitter's existing queue location, so queued work survives upgrades."""
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "microcosm" / "telemetry" / "events.sqlite3"


def default_handover_database_path(spool_path: Path) -> Path:
    """The Hugging Face module's hand-over database, next to the spool database.

    ``events.sqlite3`` gives ``events.handovers.sqlite3``. It is a separate
    file because released emitters, which Microcosm builds pin and which
    share the spool database, refuse a spool database whose migration
    revision they don't know.
    """
    return spool_path.with_name(f"{spool_path.stem}.handovers.sqlite3")
