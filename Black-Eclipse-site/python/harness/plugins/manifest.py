"""The strongly-typed, versioned plugin manifest and its validation.

A manifest is the contract between a plugin and the host. It is validated in
full before a plugin is ever loaded, so a malformed or dishonest manifest is
rejected with a named reason instead of failing later at runtime.

Validation covers: identifier shape, semantic version, SDK compatibility, the
closed permission vocabulary, contribution naming/duplication, dependency
declarations, config schemas, and host-compatibility constraints. Nothing is
"best effort": an unknown permission, an unparseable version or a duplicate
contribution is a hard rejection.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import semver
from .permissions import unknown_permissions

# The SDK API version this runtime implements. A manifest's `sdk.version` range
# MUST include it, otherwise the plugin was built against an incompatible API.
SDK_VERSION = "1.0.0"

# The runtime (Eclipse) version used for the `eclipse.min_version` compat check.
ECLIPSE_VERSION = "1.0.0"

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_CONTRIB_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]*$")
_EVENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]*[a-z0-9])?(?:\*)?$")
_SERVICE_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")

CONFIG_TYPES = {"string", "number", "boolean", "enum", "array", "object", "secret"}


class Publisher(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: Optional[str] = None
    url: Optional[str] = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v or ""):
            raise ValueError(f"publisher.id must match {_ID_RE.pattern}")
        return v


class EclipseRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_version: Optional[str] = None
    max_version: Optional[str] = None

    @field_validator("min_version", "max_version")
    @classmethod
    def _ver(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not semver.is_valid(v):
            raise ValueError(f"invalid semantic version: {v!r}")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> "EclipseRequirement":
        if self.min_version and self.max_version and semver.compare(self.min_version, self.max_version) > 0:
            raise ValueError("eclipse.min_version must not exceed eclipse.max_version")
        return self


class SDKRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = f">={SDK_VERSION}"

    @field_validator("version")
    @classmethod
    def _range(cls, v: str) -> str:
        if not _range_is_valid(v):
            raise ValueError(f"invalid SDK version range: {v!r}")
        return v


class RuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["python", "declarative"] = "python"
    # `module:attribute` relative to the plugin package, or a path to a .py file.
    entrypoint: Optional[str] = None

    @model_validator(mode="after")
    def _entrypoint_required(self) -> "RuntimeSpec":
        if self.type == "python" and not self.entrypoint:
            raise ValueError("runtime.entrypoint is required for a python plugin")
        if self.entrypoint and ":" not in self.entrypoint and not self.entrypoint.endswith(".py"):
            raise ValueError("runtime.entrypoint must be 'module:attribute' or a .py path")
        return self


class ServiceDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    version: int = 1

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not _SERVICE_KEY_RE.match(v or ""):
            raise ValueError(f"service key must match {_SERVICE_KEY_RE.pattern}")
        return v


class ServiceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    optional: bool = False

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not _SERVICE_KEY_RE.match(v or ""):
            raise ValueError(f"service key must match {_SERVICE_KEY_RE.pattern}")
        return v


class Dependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: str = "*"
    optional: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v or ""):
            raise ValueError(f"dependency id must match {_ID_RE.pattern}")
        return v

    @field_validator("version")
    @classmethod
    def _range(cls, v: str) -> str:
        if not _range_is_valid(v):
            raise ValueError(f"invalid dependency version range: {v!r}")
        return v


# ── contribution declarations ────────────────────────────────────────────────


class ToolContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    permission: Literal["read", "write", "external_action"] = "read"
    timeout_ms: int = 20_000
    deprecated: bool = False
    replacement: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _CONTRIB_NAME_RE.match(v or ""):
            raise ValueError(f"tool name must match {_CONTRIB_NAME_RE.pattern}")
        return v


class SkillContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""


class HookContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


class EventContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscribes: list[str] = Field(default_factory=list)
    publishes: list[str] = Field(default_factory=list)

    @field_validator("subscribes", "publishes")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for pattern in v:
            if not _EVENT_RE.match(pattern or ""):
                raise ValueError(f"invalid event pattern: {pattern!r}")
        return v


class CommandContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""


class UIContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["page", "panel", "sidebar", "widget", "dashboard_card", "settings", "command_palette"]
    id: str
    title: str = ""
    entry: Optional[str] = None


class WorkerContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    interval_seconds: int = 0


class WebhookContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str = ""
    # "inbound" = Eclipse receives; "outbound" = the plugin delivers to a target.
    direction: Literal["inbound", "outbound"] = "inbound"


class ResearchProviderContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kinds: list[str] = Field(default_factory=list)


class DataProviderContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kinds: list[str] = Field(default_factory=list)


class AgentContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str = ""


class WorkflowContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str


class PromptContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    scope: str = ""
    order: int = 100


class ModelContribution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    provider: str = ""


class Contributes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tools: list[ToolContribution] = Field(default_factory=list)
    skills: list[SkillContribution] = Field(default_factory=list)
    prompts: list[PromptContribution] = Field(default_factory=list)
    models: list[ModelContribution] = Field(default_factory=list)
    hooks: list[HookContribution] = Field(default_factory=list)
    events: Optional[EventContribution] = None
    commands: list[CommandContribution] = Field(default_factory=list)
    ui: list[UIContribution] = Field(default_factory=list)
    workflows: list[WorkflowContribution] = Field(default_factory=list)
    agents: list[AgentContribution] = Field(default_factory=list)
    research_providers: list[ResearchProviderContribution] = Field(default_factory=list)
    data_providers: list[DataProviderContribution] = Field(default_factory=list)
    webhooks: list[WebhookContribution] = Field(default_factory=list)
    workers: list[WorkerContribution] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_duplicate_names(self) -> "Contributes":
        for field_name in ("tools", "skills", "prompts", "models", "hooks", "commands", "ui", "workers", "webhooks", "agents"):
            items = getattr(self, field_name)
            key = "id" if field_name in ("ui", "webhooks", "agents", "prompts") else "name"
            names = [getattr(i, key) for i in items]
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                raise ValueError(f"duplicate {field_name} contribution(s): {', '.join(dupes)}")
        return self

    def all_contribution_names(self) -> dict[str, list[str]]:
        return {
            "tools": [t.name for t in self.tools],
            "skills": [s.name for s in self.skills],
            "prompts": [p.id for p in self.prompts],
            "models": [m.name for m in self.models],
            "hooks": [h.name for h in self.hooks],
            "commands": [c.name for c in self.commands],
            "ui": [u.id for u in self.ui],
            "workers": [w.name for w in self.workers],
            "webhooks": [w.id for w in self.webhooks],
            "agents": [a.id for a in self.agents],
            "research_providers": [r.name for r in self.research_providers],
            "data_providers": [d.name for d in self.data_providers],
            "workflows": [w.id for w in self.workflows],
        }


class ConfigField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    type: Literal["string", "number", "boolean", "enum", "array", "object", "secret"] = "string"
    title: str = ""
    description: str = ""
    default: Any = None
    required: bool = False
    enum: Optional[list[Any]] = None
    items: Optional[str] = None
    properties: Optional[dict[str, "ConfigField"]] = None

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_.]*$", v or ""):
            raise ValueError(f"invalid config key: {v!r}")
        return v

    @model_validator(mode="after")
    def _enum_required(self) -> "ConfigField":
        if self.type == "enum" and not self.enum:
            raise ValueError(f"config field {self.key!r} of type 'enum' requires an 'enum' list")
        return self


class AuthenticationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["none", "api_key", "bearer", "basic", "oauth2"] = "none"
    provider: Optional[str] = None
    # For oauth2: the endpoints and scopes (client id/secret live in secrets).
    authorization_url: Optional[str] = None
    token_url: Optional[str] = None
    # The grant this plugin prefers: authorization_code (PKCE, browser),
    # device_code (RFC 8628), or client_credentials (server-to-server).
    grant: Literal["authorization_code", "device_code", "client_credentials"] = "authorization_code"
    device_authorization_url: Optional[str] = None
    # Where the authorization server sends the browser back (authorization_code).
    redirect_uri: Optional[str] = None
    token_endpoint_auth_method: Literal["client_secret_post", "client_secret_basic", "none"] = "client_secret_post"
    audience: Optional[str] = None
    scopes: list[str] = Field(default_factory=list)
    # The config/secret keys this flow uses.
    api_key_field: str = "apiKey"
    username_field: str = "username"
    password_field: str = "password"

    @model_validator(mode="after")
    def _oauth_requires_urls(self) -> "AuthenticationSpec":
        if self.type == "oauth2":
            if not self.token_url:
                raise ValueError("oauth2 authentication requires token_url")
            if self.grant == "authorization_code" and not self.authorization_url:
                raise ValueError("oauth2 authorization_code grant requires authorization_url")
            if self.grant == "device_code" and not self.device_authorization_url:
                raise ValueError("oauth2 device_code grant requires device_authorization_url")
        return self


class StorageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kv: bool = True
    files: bool = False
    sqlite: bool = False


class CompatibilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    os: Optional[list[str]] = None  # "win32" | "linux" | "darwin"
    python: Optional[str] = None


class PluginManifest(BaseModel):
    """The full, validated manifest. `extra=forbid` means an unknown top-level
    key is rejected rather than silently ignored."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    version: str
    publisher: Optional[Union[Publisher, str]] = None
    description: str = ""
    category: str = ""
    tags: list[str] = Field(default_factory=list)

    eclipse: EclipseRequirement = Field(default_factory=EclipseRequirement)
    sdk: SDKRequirement = Field(default_factory=SDKRequirement)
    runtime: RuntimeSpec
    permissions: list[str] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    provides: list[ServiceDeclaration] = Field(default_factory=list)
    requires: list[ServiceRequirement] = Field(default_factory=list)
    contributes: Contributes = Field(default_factory=Contributes)
    configuration: list[ConfigField] = Field(default_factory=list)
    authentication: AuthenticationSpec = Field(default_factory=AuthenticationSpec)
    storage: StorageSpec = Field(default_factory=StorageSpec)
    compatibility: CompatibilitySpec = Field(default_factory=CompatibilitySpec)

    # ── validators ───────────────────────────────────────────────────────────

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _ID_RE.match(v or ""):
            raise ValueError(f"plugin id must match {_ID_RE.pattern}")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        if not semver.is_valid(v):
            raise ValueError(f"plugin version must be a semantic version, got {v!r}")
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not (v or "").strip():
            raise ValueError("plugin name must not be empty")
        return v.strip()

    @field_validator("permissions")
    @classmethod
    def _permissions(cls, v: list[str]) -> list[str]:
        unknown = unknown_permissions(v)
        if unknown:
            raise ValueError(f"unknown permission(s): {', '.join(unknown)}")
        return sorted(set(v))

    @model_validator(mode="after")
    def _check(self) -> "PluginManifest":
        self._check_sdk_compat()
        self._check_dependencies()
        self._check_config()
        self._check_services()
        return self

    def _check_services(self) -> None:
        for label, keys in (
            ("provides", [item.key for item in self.provides]),
            ("requires", [item.key for item in self.requires]),
        ):
            dupes = sorted({key for key in keys if keys.count(key) > 1})
            if dupes:
                raise ValueError(f"duplicate {label} service(s): {', '.join(dupes)}")
        overlap = sorted({item.key for item in self.provides} & {item.key for item in self.requires})
        if overlap:
            raise ValueError(f"a plugin cannot both provide and require: {', '.join(overlap)}")

    def _check_sdk_compat(self) -> None:
        if not semver.satisfies(SDK_VERSION, self.sdk.version):
            raise ValueError(
                f"plugin requires SDK {self.sdk.version} but this runtime provides {SDK_VERSION}"
            )

    def _check_dependencies(self) -> None:
        seen: set[str] = set()
        for dep in self.dependencies:
            if dep.id == self.id:
                raise ValueError("a plugin cannot depend on itself")
            if dep.id in seen:
                raise ValueError(f"duplicate dependency {dep.id!r}")
            seen.add(dep.id)

    def _check_config(self) -> None:
        keys = [f.key for f in self.configuration]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ValueError(f"duplicate configuration key(s): {', '.join(dupes)}")

    # ── helpers ──────────────────────────────────────────────────────────────

    @property
    def publisher_id(self) -> str:
        if isinstance(self.publisher, Publisher):
            return self.publisher.id
        return self.publisher or "unknown"

    @property
    def sdk_version(self) -> str:
        return SDK_VERSION

    def check_eclipse_version(self, version: str = ECLIPSE_VERSION) -> Optional[str]:
        """Return an incompatibility reason for the host version, or None."""
        if self.eclipse.min_version and semver.compare(version, self.eclipse.min_version) < 0:
            return f"requires Eclipse >= {self.eclipse.min_version} (host is {version})"
        if self.eclipse.max_version and semver.compare(version, self.eclipse.max_version) > 0:
            return f"requires Eclipse <= {self.eclipse.max_version} (host is {version})"
        return None

    def secret_keys(self) -> list[str]:
        """Config keys of type secret (their values are stored encrypted)."""
        return [f.key for f in self.configuration if f.type == "secret"]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def _range_is_valid(expr: str) -> bool:
    if not expr or expr.strip() in ("*", "x", "X"):
        return True
    for alt in expr.split("||"):
        for token in alt.replace(",", " ").split():
            probe = token
            stripped = re.sub(r"^[<>=^~]*", "", probe).strip()
            if stripped in ("", "x", "X", "*"):
                continue
            if re.search(r"[xX*]", stripped):
                parts = [p for p in stripped.split(".") if p not in ("x", "X", "*")]
                if any(not p.isdigit() for p in parts):
                    return False
                continue
            if not semver.is_valid(stripped if stripped.count(".") == 2 else _pad(stripped)):
                return False
    return True


def _pad(raw: str) -> str:
    parts = raw.split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def parse_manifest(data: dict[str, Any]) -> PluginManifest:
    """Validate a raw manifest dict. Raises `ValueError` on any problem."""
    return PluginManifest.model_validate(data)


ConfigField.model_rebuild()

__all__ = [
    "SDK_VERSION",
    "ECLIPSE_VERSION",
    "CONFIG_TYPES",
    "PluginManifest",
    "Contributes",
    "ConfigField",
    "AuthenticationSpec",
    "StorageSpec",
    "CompatibilitySpec",
    "Dependency",
    "RuntimeSpec",
    "parse_manifest",
    "semver",
]
