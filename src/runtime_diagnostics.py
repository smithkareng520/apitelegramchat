"""Low-overhead process/runtime diagnostics for production restarts."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


def memory_snapshot() -> dict[str, Any]:
    """Return RSS + cgroup memory information without optional dependencies."""
    rss_kb: int | None = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass

    # cgroup v2 is standard on current managed container hosts.
    limit = _read_int("/sys/fs/cgroup/memory.max")
    current = _read_int("/sys/fs/cgroup/memory.current")
    if limit == 2**63 - 1:
        limit = None

    return {
        "rss_mb": None if rss_kb is None else round(rss_kb / 1024, 1),
        "cgroup_current_mb": None if current is None else round(current / 1024 / 1024, 1),
        "cgroup_limit_mb": None if limit is None else round(limit / 1024 / 1024, 1),
        "pid": os.getpid(),
        "ppid": os.getppid(),
    }


def format_memory(snapshot: dict[str, Any] | None = None) -> str:
    s = snapshot or memory_snapshot()
    return (
        f"rss={s.get('rss_mb')}MB "
        f"cgroup={s.get('cgroup_current_mb')}MB/" 
        f"{s.get('cgroup_limit_mb')}MB pid={s.get('pid')} ppid={s.get('ppid')}"
    )
