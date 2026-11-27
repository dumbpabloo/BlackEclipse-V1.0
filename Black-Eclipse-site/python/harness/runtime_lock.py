"""One runtime process per port.

Two Python runtimes pointed at the same `agent-memory.db` fight for its write
lock and, worse, the second one's boot reconciliation can mark the first one's
in-flight runs as orphans. The front door used to start a rival process whenever
a readiness probe timed out; the two then raced on the same port (one losing the
bind only AFTER its lifespan ran).

This module owns the cheap, side-effect-free guard: an exclusive file lock keyed
by the runtime port. `main()` acquires it BEFORE uvicorn runs the lifespan, so a
second process exits without touching the database or the run registry.

The lock file lives in the runtime state dir (or the temp dir) and is held for
the process lifetime. It is intentionally advisory across hosts and hardened for
both Windows (`msvcrt`) and POSIX (`fcntl`).
"""

from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Optional

log = logging.getLogger("harness.runtime_lock")

_PORT_LOCKS: dict[str, Any] = {}


def _lock_path(port: int) -> str:
    state_dir = (
        os.environ.get("BE_RUNTIME_DIR")
        or os.environ.get("BE_STATE_DIR")
        or os.path.join(tempfile.gettempdir(), "black-eclipse-runtime")
    )
    try:
        os.makedirs(state_dir, exist_ok=True)
    except OSError:
        state_dir = tempfile.gettempdir()
    return os.path.join(state_dir, f"runtime-{int(port)}.lock")


def acquire_instance_lock(port: int) -> Optional[Any]:
    """Take the exclusive runtime lock for `port`, or return None if held.

    The returned handle must stay referenced for the process lifetime; dropping
    it releases the lock. `main()` keeps it in `_PORT_LOCKS`. Idempotent within a
    process (module import and `main()` may both call it).
    """
    existing = _PORT_LOCKS.get(str(port))
    if existing is not None:
        return existing
    path = _lock_path(port)
    handle = open(path, "a+", encoding="utf-8")  # noqa: SIM115 - held for lifetime
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        log.error(
            "another Eclipse runtime already owns port %s (%s); refusing to start a rival process",
            port, path,
        )
        return None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
    except OSError:
        pass
    _PORT_LOCKS[str(port)] = handle
    return handle


def release_instance_lock(port: int) -> None:
    """Release the lock (tests and an orderly shutdown)."""
    handle = _PORT_LOCKS.pop(str(port), None)
    if handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


def enforce_instance_lock() -> None:
    """Boot-time guard: one process per port, but ONLY when the front door asks.

    The launcher sets `BE_ENFORCE_SINGLE_RUNTIME=1` so a rival uvicorn exits at
    import time, before its lifespan can reconcile the live runtime's runs as
    orphans. Tests and embedded `TestClient` uses leave it unset (they import
    this module many times in one process).
    """
    if (os.environ.get("BE_ENFORCE_SINGLE_RUNTIME") or "").strip() not in ("1", "true", "yes"):
        return
    try:
        port = int(os.environ.get("HARNESS_PY_PORT") or 8765)
    except (TypeError, ValueError):
        port = 8765
    if acquire_instance_lock(port) is None:
        raise SystemExit(
            f"another Eclipse runtime already owns port {port}; refusing to start a rival process"
        )


__all__ = ["acquire_instance_lock", "enforce_instance_lock", "release_instance_lock"]
