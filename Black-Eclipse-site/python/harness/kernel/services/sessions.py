"""Read-only kernel session service over the existing persistence store.

The kernel does not own sessions. This is a thin provider over
``AgentMemoryStore`` so a composition row or inspection surface can read
session/run state through the kernel contract without importing persistence.
With no attached store every read is an honest empty result (never fabricated).
"""

from __future__ import annotations

from typing import Any, Optional


class SessionService:
    def __init__(self, store: Any = None) -> None:
        self._store = store

    def attach(self, store: Any) -> None:
        self._store = store

    @property
    def available(self) -> bool:
        return self._store is not None

    def _call(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self._store is None:
            return None
        method = getattr(self._store, name, None)
        if method is None:
            return None
        return method(*args, **kwargs)

    def get(self, session_id: str) -> Optional[dict[str, Any]]:
        return self._call("get_chat_session", session_id)

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._call("list_chat_sessions", limit=limit)
        return list(rows or [])

    def runs(self, session_id: str) -> list[str]:
        rows = self._call("session_runs", session_id)
        return [str(row) for row in (rows or [])]

    def active_run(self, session_id: str) -> Optional[dict[str, Any]]:
        return self._call("active_session_run", session_id)

    def current(self, run_id: str) -> Optional[dict[str, Any]]:
        return self._call("get_run", run_id)

    def describe(self) -> dict[str, Any]:
        return {"available": self.available, "provider": "AgentMemoryStore"}
