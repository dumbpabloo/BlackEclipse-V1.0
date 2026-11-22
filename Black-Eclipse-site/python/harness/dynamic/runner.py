"""Child process for a dynamic package.

Reads newline-delimited JSON requests from stdin and writes one JSON response per
line. The package is a single `module.py` in the child's working directory and
must expose `handle(payload) -> JSON value`.

The child is launched by `harness.code.sandbox.spawn` (scrubbed env, process-tree
kill, wall-clock/output limits). It imports only the package and stdlib; no host
services are reachable from here.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys


def _load(path: str):
    spec = importlib.util.spec_from_file_location("dynamic_package", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("the package module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    handler = getattr(module, "handle", None)
    if not callable(handler):
        raise RuntimeError("the package module must define handle(payload)")
    return handler


def main() -> int:
    directory = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    try:
        handler = _load(os.path.join(directory, "module.py"))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": f"invalid request: {exc}"}), flush=True)
            continue
        try:
            value = handler(request.get("payload") or {})
            print(json.dumps({"id": request.get("id"), "ok": True, "result": value}, default=str), flush=True)
        except Exception as exc:  # noqa: BLE001 - a package failure is a response
            print(json.dumps({"id": request.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
