"""Monitor a process identity, not just a reusable numeric PID."""

import os

import psutil


class ParentProcess:
    def __init__(self, pid: int, created_at: float):
        self.pid = pid
        self.created_at = created_at

    def alive(self) -> bool:
        try:
            process = psutil.Process(self.pid)
            return (
                process.create_time() == self.created_at
                and process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
            )
        except (psutil.Error, OSError):
            return False


def parent_process(pid: int, created_at: float | None) -> ParentProcess:
    """Identify the build that owns this host.

    A launcher may pass the creation time it measured. Otherwise the named
    process must still be this host's parent, which also proves it has not
    exited and been replaced by an unrelated process with a reused PID.
    """
    if created_at is not None:
        return ParentProcess(pid, created_at)
    if os.getppid() != pid:
        raise ValueError("the named parent process is not this host's parent")
    return ParentProcess(pid, psutil.Process(pid).create_time())
