"""Canvas contracts — request bodies, edit operations and API responses.

Closed vocabularies only. The model never authors a free-form patch format: it
must return one or more typed operations that this module validates.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

CANVAS_METADATA_KEY = "canvas"
CANVAS_SCHEMA_VERSION = 1
MAX_CANVAS_BYTES = 2 * 1024 * 1024
MAX_EDIT_CONTENT_CHARS = 120_000
MAX_OPERATIONS = 40

_CFG = ConfigDict(alias_generator=to_camel, populate_by_name=True)
_CFG_FORBID = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class CanvasKind(str, Enum):
    DOCUMENT = "document"
    CODE = "code"
    VISUAL = "visual"
    PRESENTATION = "presentation"


class CanvasMode(str, Enum):
    ASK = "ask"
    EDIT = "edit"
    REVIEW = "review"
    BUILD = "build"


class CanvasOp(str, Enum):
    APPEND = "append"
    REPLACE_TEXT_RANGE = "replace_text_range"
    REPLACE_SECTION = "replace_section"
    INSERT_AFTER_SECTION = "insert_after_section"
    REPLACE_DOCUMENT = "replace_document"
    APPLY_CODE_PATCH = "apply_code_patch"


class EditOperation(BaseModel):
    """One typed, reviewable edit. Exactly the fields the op needs are used."""

    model_config = _CFG_FORBID

    op: CanvasOp
    content: str = ""
    start: Optional[int] = Field(default=None, ge=0)
    end: Optional[int] = Field(default=None, ge=0)
    expected_text: Optional[str] = None
    heading: Optional[str] = None
    search: Optional[str] = None
    replace: Optional[str] = None
    explanation: str = Field(default="", max_length=2_000)

    @field_validator("content", "replace", "search", "expected_text", "heading")
    @classmethod
    def _bounded_text(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value) > MAX_EDIT_CONTENT_CHARS:
            raise ValueError("operation text exceeds the canvas limit")
        return value


class Selection(BaseModel):
    model_config = _CFG

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str = ""

    @field_validator("end")
    @classmethod
    def _ordered(cls, value: int, info: Any) -> int:
        start = info.data.get("start")
        if start is not None and value < start:
            raise ValueError("selection end must not precede start")
        return value


class CreateCanvasInput(BaseModel):
    model_config = _CFG

    name: str = Field(min_length=1, max_length=200)
    kind: CanvasKind = CanvasKind.DOCUMENT
    language: Optional[str] = Field(default=None, max_length=40)
    content: str = ""
    description: str = Field(default="", max_length=4_000)
    conversation_id: Optional[str] = None
    collection_id: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SaveCanvasInput(BaseModel):
    """A save is guarded by `baseVersion` (the version the editor loaded).

    `operations` is preferred for AI-assisted saves: the server re-applies the
    typed operations to the current bytes instead of trusting client content.
    """

    model_config = _CFG

    base_version: int = Field(ge=1)
    content: Optional[str] = None
    # Text ops (EditOperation) for documents/code/visual, deck ops (DeckOp) for
    # presentations. Validated by kind in CanvasService — a route cannot smuggle
    # a text operation into a deck.
    operations: Optional[list[Any]] = None
    change_note: str = Field(default="", max_length=1_000)
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


class RestoreCanvasInput(BaseModel):
    model_config = _CFG

    base_version: int = Field(ge=1)
    version: int = Field(ge=1)
    change_note: str = Field(default="", max_length=1_000)
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


class CanvasEditRequest(BaseModel):
    model_config = _CFG

    base_version: int = Field(ge=1)
    instruction: str = Field(min_length=1, max_length=8_000)
    mode: CanvasMode = CanvasMode.EDIT
    selection: Optional[Selection] = None
    model_settings: Optional[dict[str, Any]] = None

    @field_validator("instruction")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("instruction must not be blank")
        return stripped


class CanvasVersionSummary(BaseModel):
    model_config = _CFG

    version_number: int
    change_note: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: str
    created_by: Optional[str] = None


class CanvasSnapshot(BaseModel):
    model_config = _CFG

    artifact_id: str
    name: str
    description: str = ""
    kind: CanvasKind
    language: Optional[str] = None
    mime_type: str
    content: str
    version: int
    size_bytes: Optional[int] = None
    updated_at: str
    conversation_id: Optional[str] = None
    versions: list[CanvasVersionSummary] = Field(default_factory=list)


class CanvasProposal(BaseModel):
    """A validated, not-yet-applied AI edit."""

    model_config = _CFG

    artifact_id: str
    base_version: int
    current_version: int
    mode: CanvasMode
    answer: str = ""
    summary: str = ""
    operations: list[EditOperation] = Field(default_factory=list)
    # Deck ops (the `op` vocabulary in presentations/ops.py) for a presentation
    # artifact. Exactly one of `operations` / `deck_operations` is non-empty.
    deck_operations: list[dict[str, Any]] = Field(default_factory=list)
    result_content: Optional[str] = None
    notes: list[str] = Field(default_factory=list)


class CanvasListItem(BaseModel):
    model_config = _CFG

    artifact_id: str
    name: str
    kind: CanvasKind
    language: Optional[str] = None
    description: str = ""
    version: int
    size_bytes: Optional[int] = None
    updated_at: str
    conversation_id: Optional[str] = None


class CanvasError(Exception):
    code = "CANVAS_ERROR"
    status = 400

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class CanvasNotFound(CanvasError):
    code = "CANVAS_NOT_FOUND"
    status = 404


class CanvasConflict(CanvasError):
    """A save/edit was based on a revision that is no longer current."""

    code = "CANVAS_CONFLICT"
    status = 409

    def __init__(self, message: str, *, current_version: int) -> None:
        super().__init__(message)
        self.current_version = current_version


class CanvasUnavailable(CanvasError):
    code = "CANVAS_UNAVAILABLE"
    status = 503


__all__ = [
    "CANVAS_METADATA_KEY",
    "CANVAS_SCHEMA_VERSION",
    "MAX_CANVAS_BYTES",
    "MAX_EDIT_CONTENT_CHARS",
    "MAX_OPERATIONS",
    "CanvasConflict",
    "CanvasEditRequest",
    "CanvasError",
    "CanvasKind",
    "CanvasListItem",
    "CanvasMode",
    "CanvasNotFound",
    "CanvasOp",
    "CanvasProposal",
    "CanvasSnapshot",
    "CanvasUnavailable",
    "CanvasVersionSummary",
    "CreateCanvasInput",
    "EditOperation",
    "RestoreCanvasInput",
    "SaveCanvasInput",
    "Selection",
]
