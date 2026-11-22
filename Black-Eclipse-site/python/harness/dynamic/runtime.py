"""Process-wide dynamic package service."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .service import DynamicPackages

_SINGLETON: Optional[DynamicPackages] = None


def dynamic_packages(*, fresh: bool = False) -> DynamicPackages:
    global _SINGLETON
    if _SINGLETON is not None and not fresh:
        return _SINGLETON
    root = os.environ.get("ECLIPSE_DYNAMIC_DIR") or (Path.home() / ".black-eclipse" / "dynamic")
    _SINGLETON = DynamicPackages(
        root,
        ttl_seconds=int(os.environ.get("ECLIPSE_DYNAMIC_TTL_SECONDS", "3600") or 3600),
    )
    return _SINGLETON


def reset_dynamic() -> None:
    global _SINGLETON
    _SINGLETON = None
