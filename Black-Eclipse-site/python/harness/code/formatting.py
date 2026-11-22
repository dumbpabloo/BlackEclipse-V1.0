"""Post-write formatting: run the PROJECT's formatter on the file just changed.

A coding model returns correct code in slightly wrong whitespace. Without a
formatter the saved file keeps that shape, every later diff carries it, and a
project's `prettier --check` / `ruff format --check` gate fails on an edit that
was semantically fine.

This module is deliberately narrow:

  * it recognises only the project's own formatter (a local prettier dependency,
    a configured ruff, gofmt, a configured rustfmt) - it never downloads or
    guesses one;
  * it formats ONE file, the file that was just written;
  * it is bounded (10 s, 1 MiB), never fatal, and reports exactly what it did or
    why it skipped;
  * `BE_FORMAT_ON_EDIT=0` turns it off for an operator.

The command runs through the same sandbox primitive as every other child
process (scrubbed environment, no network, output cap). When that primitive is
unavailable, formatting is skipped with a reason - never run unsandboxed.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

FORMAT_TIMEOUT_S = 10
MAX_FORMAT_BYTES = 1_000_000
MAX_ERROR_CHARS = 400

_PRETTIER_EXTENSIONS = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".jsonc",
    ".css", ".scss", ".less", ".md", ".mdx", ".yaml", ".yml", ".html",
    ".vue", ".svelte",
})
_RUFF_EXTENSIONS = frozenset({".py", ".pyi"})
_RUSTFMT_CONFIGS = ("rustfmt.toml", ".rustfmt.toml")
_PRETTIER_CONFIGS = (
    ".prettierrc", ".prettierrc.json", ".prettierrc.yaml", ".prettierrc.yml",
    ".prettierrc.json5", ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.mjs",
    "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs",
)


@dataclass(frozen=True)
class Formatter:
    name: str
    argv: tuple[str, ...]
    cwd: Path


def enabled() -> bool:
    raw = str(os.environ.get("BE_FORMAT_ON_EDIT", "1")).strip().lower()
    return raw not in {"0", "off", "false", "no", "disable", "disabled"}


def _package_json(root: Path) -> dict[str, Any]:
    import json  # noqa: PLC0415

    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - no manifest is not a failure
        return {}


def _has_prettier_dependency(root: Path) -> bool:
    package = _package_json(root)
    for key in ("dependencies", "devDependencies", "optionalDependencies"):
        section = package.get(key)
        if isinstance(section, dict) and "prettier" in section:
            return True
    return False


def _prettier_binary(root: Path) -> Optional[tuple[str, ...]]:
    """A runnable prettier argv prefix, or None.

    Prefer `node node_modules/prettier/bin/prettier.cjs`: it works on Windows
    without the `.cmd` shim (which CreateProcess cannot execute directly).
    """
    local_js = root / "node_modules" / "prettier" / "bin" / "prettier.cjs"
    node = shutil.which("node")
    if local_js.exists() and node:
        return (node, str(local_js))
    found = shutil.which("prettier")
    if not found:
        return None
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return (comspec, "/d", "/s", "/c", found)
    return (found,)


def _ruff_configured(root: Path) -> bool:
    if (root / "ruff.toml").exists() or (root / ".ruff.toml").exists():
        return True
    for name in ("pyproject.toml", "setup.cfg"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "[tool.ruff" in text or "[ruff]" in text:
            return True
    return False


def detect(root: Path, rel_path: str, target: Optional[Path] = None) -> Optional[Formatter]:
    """The project's formatter for this file, or None. Never invents one."""
    root = Path(root)
    suffix = Path(rel_path).suffix.lower()
    arg = str(target) if target is not None else rel_path
    if suffix in _PRETTIER_EXTENSIONS:
        configured = (
            any((root / name).exists() for name in _PRETTIER_CONFIGS)
            or _has_prettier_dependency(root)
        )
        argv = _prettier_binary(root) if configured else None
        if argv:
            return Formatter("prettier", (*argv, "--write", arg), root)
    if suffix in _RUFF_EXTENSIONS and _ruff_configured(root):
        found = shutil.which("ruff")
        if found:
            return Formatter("ruff", (found, "format", "--", arg), root)
    if suffix == ".go":
        found = shutil.which("gofmt")
        if found:
            return Formatter("gofmt", (found, "-w", arg), root)
    if suffix == ".rs" and any((root / name).exists() for name in _RUSTFMT_CONFIGS):
        found = shutil.which("rustfmt")
        if found:
            return Formatter("rustfmt", (found, "--edition", "2021", arg), root)
    return None


def _run(formatter: Formatter) -> tuple[Optional[int], str]:
    """Run one formatter through the sandbox. Returns `(exit_code, output_tail)`."""
    from ..sandbox import RunSpec, SandboxUnavailable  # noqa: PLC0415
    from ..sandbox import run as sandbox_run  # noqa: PLC0415
    from ..sandbox import build_env  # noqa: PLC0415

    spec = RunSpec(
        argv=list(formatter.argv),
        cwd=formatter.cwd,
        env=build_env(),
        timeout_s=FORMAT_TIMEOUT_S,
        output_cap_bytes=20_000,
        network=False,
    )
    try:
        raw = sandbox_run(spec)
    except SandboxUnavailable:
        return None, "SANDBOX_UNAVAILABLE"
    output = str(getattr(raw, "output", "") or "")[-MAX_ERROR_CHARS:]
    return getattr(raw, "exit_code", None), output


def maybe_format(
    target: Path, rel_path: str, *,
    runner: Optional[Callable[[Formatter], tuple[Optional[int], str]]] = None,
) -> dict[str, Any]:
    """Format one saved file with the project's formatter. Never raises."""
    if not enabled():
        return {"status": "disabled", "reason": "BE_FORMAT_ON_EDIT is off"}
    target = Path(target)
    try:
        if not target.exists() or target.stat().st_size > MAX_FORMAT_BYTES:
            return {"status": "skipped", "reason": "file missing or too large to format"}
        root = _project_root(target) or target.parent
        formatter = detect(root, rel_path, target)
        if formatter is None:
            return {"status": "skipped", "reason": "no project formatter configured"}
        before = target.read_bytes()
        started = time.monotonic()
        code, output = (runner or _run)(formatter)
        elapsed = int((time.monotonic() - started) * 1000)
        after = target.read_bytes() if target.exists() else b""
        changed = before != after
        if code is None:
            return {
                "status": "skipped", "formatter": formatter.name,
                "reason": output or "formatter could not run", "durationMs": elapsed,
            }
        if code != 0:
            return {
                "status": "failed", "formatter": formatter.name, "exitCode": code,
                "reason": (output or "the formatter exited non-zero")[:MAX_ERROR_CHARS],
                "durationMs": elapsed, "changed": changed,
            }
        return {
            "status": "ok", "formatter": formatter.name, "changed": changed,
            "durationMs": elapsed,
        }
    except Exception as exc:  # noqa: BLE001 - formatting is never fatal
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"[:MAX_ERROR_CHARS]}


def _project_root(target: Path) -> Optional[Path]:
    """The nearest ancestor holding a manifest, bounded to 12 levels."""
    current = target.parent
    for _ in range(12):
        if any((current / name).exists() for name in ("package.json", "pyproject.toml", "go.mod", "Cargo.toml")):
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


__all__ = [
    "FORMAT_TIMEOUT_S",
    "MAX_FORMAT_BYTES",
    "Formatter",
    "detect",
    "enabled",
    "maybe_format",
]
