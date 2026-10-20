"""Where the emitter keeps its durable queue."""

import os
from pathlib import Path


def default_spool_path() -> Path:
    """The emitter's existing queue location, so queued work survives upgrades."""
    configured = os.environ.get("XDG_CACHE_HOME", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".cache"
    return root / "microcosm" / "telemetry" / "events.sqlite3"
