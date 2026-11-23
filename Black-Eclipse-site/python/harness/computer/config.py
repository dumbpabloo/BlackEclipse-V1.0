"""The `computer_use:` configuration section.

Eclipse has no YAML config file; configuration is env-var driven with a dataclass
default, exactly like the browser subsystem. An optional JSON override at
`<app>/.black-eclipse/computer.json` lets an operator express the nested
`computer_use:` object from the design without hand-writing env vars; env always
wins over the file, and the file always wins over the defaults.

This module only READS configuration. It never captures a screen or moves a
pointer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Artifacts live under the app root (the same gitignored tier as agent-memory.db),
# never the JDdS vault and never the operator's checkout.
_ARTIFACTS_RELPATH = ".black-eclipse/computer"

# Hard, non-negotiable ceilings. An operator may lower a limit but not remove it.
MAX_SESSIONS_CEILING = 8
MAX_ACTIONS_CEILING = 2000
MAX_RECORDING_FRAMES_CEILING = 2000

DELIVERY_MODES = ("background", "foreground")
OBSERVATION_MODES = ("som", "vision", "ax", "ocr")
VISION_MODES = ("local", "cloud", "hybrid")
FINANCE_MODES = ("read_only", "assisted", "autonomous", "simulation")
COMPUTER_MODES = ("dry_run", "live")
ISOLATION_MODES = ("off", "auto", "on")


def _truthy(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(value: Optional[str], default: int, *, low: int, high: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


def _choice(value: Optional[str], allowed: tuple[str, ...], default: str) -> str:
    text = (value or "").strip().lower()
    return text if text in allowed else default


def _app_root() -> Optional[Path]:
    try:
        from ..eclipse_memory.config import repo_root  # noqa: PLC0415

        resolved = repo_root()
        return Path(resolved) if resolved else None
    except Exception:  # noqa: BLE001 - a missing config is simply no app root
        return None


@dataclass
class ComputerConfig:
    """The resolved `computer_use:` configuration for this process."""

    enabled: bool = True
    # Default operation mode: background (never steal focus) unless explicitly
    # escalated. `foreground` is a higher-impact action.
    mode: str = "background"
    # dry_run: describe the action, change nothing.
    computer_mode: str = "live"
    driver_backend: str = "auto"      # auto | windows | linux | macos | null
    # Isolation: off = the shared local desktop (zero-config default); auto =
    # the platform's isolated backend (Xvfb on Linux; Windows Sandbox on Windows),
    # used only when genuinely available; on = require it (falls back if absent).
    isolation: str = "off"            # off | auto | on
    isolation_provider: str = "auto"  # auto | local | x11 | wayland | windows_sandbox | rdp
    headless_display_base: int = 99
    headless_screen: str = "1920x1080x24"
    # RDP isolation (isolation_provider=rdp): an operator-supplied host or .rdp file.
    rdp_host: str = ""
    rdp_file: str = ""
    observation_default_mode: str = "som"
    # Vision.
    vision_mode: str = "hybrid"       # local | cloud | hybrid
    vision_allow_cloud_for_sensitive: bool = False
    vision_model: Optional[str] = None
    # OCR.
    ocr_enabled: bool = True
    ocr_backend: str = "auto"         # auto | winocr | tesseract | none
    # Verification / recovery.
    verification_enabled: bool = True
    recovery_enabled: bool = True
    recovery_max_attempts: int = 3
    # Security.
    sensitive_data_redaction: bool = True
    # Automatic procedural memory: a VERIFIED workflow becomes a candidate after
    # a successful desktop run. Off by default (suggestion only); when on, the
    # candidate is also written to Eclipse-Memory, tagged as auto.
    memory_auto_capture: bool = False
    memory_min_actions: int = 2
    # Finance.
    finance_mode: str = "read_only"   # read_only | assisted | autonomous | simulation
    finance_max_order_value: float = 0.0
    # Finance allowlists (all empty ⇒ unconstrained beyond mode + absolute block).
    finance_allowed_apps: list[str] = field(default_factory=list)
    finance_allowed_accounts: list[str] = field(default_factory=list)
    finance_allowed_instruments: list[str] = field(default_factory=list)
    # Recording.
    recording_enabled: bool = True
    recording_screenshots: bool = True
    # Sessions.
    max_concurrent: int = 2
    idle_timeout_seconds: int = 900
    # Limits.
    max_actions: int = 200
    action_timeout_seconds: int = 30
    # Observation context budget (chars); over it the full snapshot is persisted.
    snapshot_max_chars: int = 24_000
    # Confirmation threshold: grounding confidence below this is AMBIGUOUS for a
    # high-risk target.
    grounding_min_confidence: float = 0.6
    # GUI grounding backend (UI-TARS-class), OpenAI-compatible.
    grounding_url: str = ""
    grounding_model: str = ""
    grounding_api_key: str = ""
    grounding_width: int = 1000
    grounding_height: int = 1000
    grounding_timeout_seconds: int = 30
    # Application policy.
    allowed_apps: list[str] = field(default_factory=list)
    denied_apps: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    prohibited_actions: list[str] = field(default_factory=list)
    # Artifact directories (all confined under `artifacts_root`).
    artifacts_root: Path = field(default_factory=Path)
    screenshots_dir: Path = field(default_factory=Path)
    snapshots_dir: Path = field(default_factory=Path)
    recordings_dir: Path = field(default_factory=Path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "computerMode": self.computer_mode,
            "driver": {"backend": self.driver_backend},
            "isolation": {
                "mode": self.isolation, "provider": self.isolation_provider,
                "displayBase": self.headless_display_base, "screen": self.headless_screen,
                "rdpHost": self.rdp_host, "rdpFile": self.rdp_file,
            },
            "observation": {"defaultMode": self.observation_default_mode},
            "vision": {
                "mode": self.vision_mode,
                "allowCloudForSensitive": self.vision_allow_cloud_for_sensitive,
            },
            "ocr": {"enabled": self.ocr_enabled, "backend": self.ocr_backend},
            "verification": {"enabled": self.verification_enabled},
            "recovery": {"enabled": self.recovery_enabled, "maxAttempts": self.recovery_max_attempts},
            "security": {"sensitiveDataRedaction": self.sensitive_data_redaction},
            "memory": {
                "autoCaptureProcedures": self.memory_auto_capture,
                "minActions": self.memory_min_actions,
            },            "finance": {
                "mode": self.finance_mode, "maxOrderValue": self.finance_max_order_value,
                "allowedApps": list(self.finance_allowed_apps),
                "allowedAccounts": list(self.finance_allowed_accounts),
                "allowedInstruments": list(self.finance_allowed_instruments),
            },
            "recording": {"enabled": self.recording_enabled, "screenshots": self.recording_screenshots},
            "limits": {
                "maxConcurrent": self.max_concurrent,
                "idleTimeoutSeconds": self.idle_timeout_seconds,
                "maxActions": self.max_actions,
                "actionTimeoutSeconds": self.action_timeout_seconds,
            },
            "snapshotMaxChars": self.snapshot_max_chars,
            "groundingMinConfidence": self.grounding_min_confidence,
            "grounding": {
                "url": self.grounding_url, "model": self.grounding_model,
                "width": self.grounding_width, "height": self.grounding_height,
                "configured": bool(self.grounding_url and self.grounding_model),
            },
            "policy": {
                "allowedApps": list(self.allowed_apps),
                "deniedApps": list(self.denied_apps),
                "allowedActions": list(self.allowed_actions),
                "prohibitedActions": list(self.prohibited_actions),
            },
            "artifactsRoot": str(self.artifacts_root),
        }


def _config_file_overrides() -> dict[str, Any]:
    """A best-effort read of `<app>/.black-eclipse/computer.json` (or env override)."""
    root = _app_root()
    if root is None:
        return {}
    override_path = os.environ.get("HARNESS_COMPUTER_CONFIG")
    path = Path(override_path) if override_path else root / ".black-eclipse" / "computer.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    block = payload.get("computer_use")
    return block if isinstance(block, dict) else payload


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, (str, int, float))]
    return []


def _csv_or_file(env: Optional[str], file_value: Any) -> list[str]:
    """A comma-separated env override when set; otherwise the file's list."""
    if env is not None:
        return [part.strip() for part in env.split(",") if part.strip()]
    return _str_list(file_value)


def load_computer_config() -> ComputerConfig:
    """Resolve the Computer Use configuration from defaults, the JSON override, env."""
    overrides = _config_file_overrides()
    observation = overrides.get("observation") if isinstance(overrides.get("observation"), dict) else {}
    vision = overrides.get("vision") if isinstance(overrides.get("vision"), dict) else {}
    ocr = overrides.get("ocr") if isinstance(overrides.get("ocr"), dict) else {}
    recovery = overrides.get("recovery") if isinstance(overrides.get("recovery"), dict) else {}
    security = overrides.get("security") if isinstance(overrides.get("security"), dict) else {}
    finance = overrides.get("finance") if isinstance(overrides.get("finance"), dict) else {}
    recording = overrides.get("recording") if isinstance(overrides.get("recording"), dict) else {}
    limits = overrides.get("limits") if isinstance(overrides.get("limits"), dict) else {}

    def top(key: str, default: Any) -> Any:
        return overrides.get(key, default)

    def nested(section: dict[str, Any], key: str, default: Any) -> Any:
        return section.get(key, overrides.get(key, default))

    app_root = _app_root()
    default_artifacts = (app_root / _ARTIFACTS_RELPATH) if app_root else Path(".black-eclipse/computer")
    raw_artifacts = os.environ.get("HARNESS_COMPUTER_ARTIFACTS_DIR")
    artifacts_root = Path(raw_artifacts).expanduser() if raw_artifacts else default_artifacts

    driver_block = overrides.get("driver") if isinstance(overrides.get("driver"), dict) else {}
    isolation_block = overrides.get("isolation") if isinstance(overrides.get("isolation"), dict) else {}

    cfg = ComputerConfig(
        enabled=_truthy(os.environ.get("HARNESS_COMPUTER_ENABLED"), bool(top("enabled", True))),
        mode=_choice(os.environ.get("HARNESS_COMPUTER_MODE") or nested(overrides, "mode", "background"), DELIVERY_MODES, "background"),
        computer_mode=_choice(os.environ.get("HARNESS_COMPUTER_RUN_MODE") or top("computer_mode", "live"), COMPUTER_MODES, "live"),
        driver_backend=str(os.environ.get("HARNESS_COMPUTER_DRIVER") or driver_block.get("backend", "auto")),
        isolation=_choice(
            os.environ.get("HARNESS_COMPUTER_ISOLATION") or isolation_block.get("mode", "off"),
            ISOLATION_MODES, "off",
        ),
        isolation_provider=str(
            os.environ.get("HARNESS_COMPUTER_ISOLATION_PROVIDER") or isolation_block.get("provider", "auto")
        ),
        headless_display_base=_int(
            os.environ.get("HARNESS_COMPUTER_DISPLAY_BASE"), int(isolation_block.get("display_base", 99)), low=1, high=9999
        ),
        headless_screen=str(isolation_block.get("screen", "1920x1080x24")),
        rdp_host=str(os.environ.get("HARNESS_COMPUTER_RDP_HOST") or isolation_block.get("rdp_host", "")),
        rdp_file=str(os.environ.get("HARNESS_COMPUTER_RDP_FILE") or isolation_block.get("rdp_file", "")),
        observation_default_mode=_choice(
            os.environ.get("HARNESS_COMPUTER_OBSERVATION") or observation.get("default_mode", "som"),
            OBSERVATION_MODES, "som",
        ),
        vision_mode=_choice(
            os.environ.get("HARNESS_COMPUTER_VISION_MODE") or vision.get("mode", "hybrid"),
            VISION_MODES, "hybrid",
        ),
        vision_allow_cloud_for_sensitive=_truthy(
            os.environ.get("HARNESS_COMPUTER_VISION_ALLOW_CLOUD_SENSITIVE"),
            bool(vision.get("allow_cloud_for_sensitive", False)),
        ),
        vision_model=os.environ.get("HARNESS_COMPUTER_VISION_MODEL") or vision.get("model"),
        ocr_enabled=_truthy(os.environ.get("HARNESS_COMPUTER_OCR_ENABLED"), bool(ocr.get("enabled", True))),
        ocr_backend=str(os.environ.get("HARNESS_COMPUTER_OCR_BACKEND") or ocr.get("backend", "auto")),
        verification_enabled=_truthy(os.environ.get("HARNESS_COMPUTER_VERIFY"), bool(overrides.get("verification", {}).get("enabled", True) if isinstance(overrides.get("verification"), dict) else True)),
        recovery_enabled=_truthy(os.environ.get("HARNESS_COMPUTER_RECOVERY"), bool(recovery.get("enabled", True))),
        recovery_max_attempts=_int(os.environ.get("HARNESS_COMPUTER_RECOVERY_ATTEMPTS"), int(recovery.get("max_attempts", 3)), low=0, high=10),
        sensitive_data_redaction=_truthy(os.environ.get("HARNESS_COMPUTER_REDACTION"), bool(security.get("sensitive_data_redaction", True))),
        memory_auto_capture=_truthy(
            os.environ.get("HARNESS_COMPUTER_MEMORY_AUTO_CAPTURE"),
            bool((overrides.get("memory") or {}).get("auto_capture", False)),
        ),
        memory_min_actions=_int(
            os.environ.get("HARNESS_COMPUTER_MEMORY_MIN_ACTIONS"),
            int((overrides.get("memory") or {}).get("min_actions", 2) or 2),
            low=1, high=50,
        ),
        finance_mode=_choice(os.environ.get("HARNESS_COMPUTER_FINANCE_MODE") or finance.get("mode", "read_only"), FINANCE_MODES, "read_only"),
        finance_max_order_value=float(finance.get("max_order_value", 0.0) or 0.0),
        finance_allowed_apps=_csv_or_file(
            os.environ.get("HARNESS_COMPUTER_FINANCE_ALLOWED_APPS"), finance.get("allowed_apps", [])
        ),
        finance_allowed_accounts=_csv_or_file(
            os.environ.get("HARNESS_COMPUTER_FINANCE_ALLOWED_ACCOUNTS"), finance.get("allowed_accounts", [])
        ),
        finance_allowed_instruments=_csv_or_file(
            os.environ.get("HARNESS_COMPUTER_FINANCE_ALLOWED_INSTRUMENTS"), finance.get("allowed_instruments", [])
        ),
        recording_enabled=_truthy(os.environ.get("HARNESS_COMPUTER_RECORDING"), bool(recording.get("enabled", True))),
        recording_screenshots=_truthy(os.environ.get("HARNESS_COMPUTER_RECORDING_SCREENSHOTS"), bool(recording.get("screenshots", True))),
        max_concurrent=_int(os.environ.get("HARNESS_COMPUTER_MAX_SESSIONS"), int(limits.get("max_concurrent", 2)), low=1, high=MAX_SESSIONS_CEILING),
        idle_timeout_seconds=_int(os.environ.get("HARNESS_COMPUTER_IDLE_TIMEOUT"), int(limits.get("idle_timeout_seconds", 900)), low=30, high=86_400),
        max_actions=_int(os.environ.get("HARNESS_COMPUTER_MAX_ACTIONS"), int(limits.get("max_actions", 200)), low=1, high=MAX_ACTIONS_CEILING),
        action_timeout_seconds=_int(os.environ.get("HARNESS_COMPUTER_ACTION_TIMEOUT"), int(limits.get("action_timeout_seconds", 30)), low=1, high=600),
        snapshot_max_chars=_int(os.environ.get("HARNESS_COMPUTER_SNAPSHOT_CHARS"), int(top("snapshot_max_chars", 24_000)), low=1_000, high=200_000),
        grounding_min_confidence=float(top("grounding_min_confidence", 0.6) or 0.6),
        grounding_url=str(
            os.environ.get("HARNESS_COMPUTER_GROUNDING_URL")
            or (overrides.get("grounding") or {}).get("url", "")
        ),
        grounding_model=str(
            os.environ.get("HARNESS_COMPUTER_GROUNDING_MODEL")
            or (overrides.get("grounding") or {}).get("model", "")
        ),
        grounding_api_key=str(
            os.environ.get("HARNESS_COMPUTER_GROUNDING_API_KEY")
            or (overrides.get("grounding") or {}).get("api_key", "")
        ),
        grounding_width=_int(
            os.environ.get("HARNESS_COMPUTER_GROUNDING_WIDTH"),
            int((overrides.get("grounding") or {}).get("width", 1000) or 1000),
            low=100, high=10_000,
        ),
        grounding_height=_int(
            os.environ.get("HARNESS_COMPUTER_GROUNDING_HEIGHT"),
            int((overrides.get("grounding") or {}).get("height", 1000) or 1000),
            low=100, high=10_000,
        ),
        grounding_timeout_seconds=_int(
            os.environ.get("HARNESS_COMPUTER_GROUNDING_TIMEOUT"),
            int((overrides.get("grounding") or {}).get("timeout_seconds", 30) or 30),
            low=1, high=120,
        ),
        allowed_apps=_str_list((overrides.get("policy") or {}).get("allowed_apps") if isinstance(overrides.get("policy"), dict) else top("allowed_apps", [])),
        denied_apps=_str_list((overrides.get("policy") or {}).get("denied_apps") if isinstance(overrides.get("policy"), dict) else top("denied_apps", [])),
        allowed_actions=_str_list((overrides.get("policy") or {}).get("allowed_actions") if isinstance(overrides.get("policy"), dict) else top("allowed_actions", [])),
        prohibited_actions=_str_list((overrides.get("policy") or {}).get("prohibited_actions") if isinstance(overrides.get("policy"), dict) else top("prohibited_actions", [])),
        artifacts_root=artifacts_root,
    )
    cfg.screenshots_dir = artifacts_root / "screenshots"
    cfg.snapshots_dir = artifacts_root / "snapshots"
    cfg.recordings_dir = artifacts_root / "recordings"
    return cfg


# ── Public helpers (the ONE place the config is resolved) ────────────────────

_CONFIG_CACHE: Optional[ComputerConfig] = None


def computer_config(refresh: bool = False) -> ComputerConfig:
    """The process-wide Computer Use configuration (resolved once, env-driven)."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is None or refresh:
        _CONFIG_CACHE = load_computer_config()
    return _CONFIG_CACHE


def computer_enabled() -> bool:
    """Whether the Computer Use toolset is enabled for this process."""
    return computer_config().enabled


__all__ = [
    "COMPUTER_MODES",
    "ComputerConfig",
    "DELIVERY_MODES",
    "FINANCE_MODES",
    "ISOLATION_MODES",
    "MAX_ACTIONS_CEILING",
    "MAX_RECORDING_FRAMES_CEILING",
    "MAX_SESSIONS_CEILING",
    "OBSERVATION_MODES",
    "VISION_MODES",
    "computer_config",
    "computer_enabled",
    "load_computer_config",
]
