"""Plugin resolution: turn a spec into a name, coeffects and an ``apply``.

Supported forms:

* a module path plus attribute - ``"mypkg.plugin:apply"``;
* an absolute or relative ``.py`` path plus attribute - ``"./plugin.py:apply"``;
* a function with signature ``(ctx)`` or ``(ctx, config)``;
* an object or class with an ``apply`` method;
* a module object with a top-level ``apply``.

A plugin declares required services with ``inject`` and optional ones with
``optional_inject``; the kernel gates activation on the required set.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import PluginRejected

DEFAULT_ATTR = "apply"


@dataclass
class ResolvedPlugin:
    name: str
    apply: Callable[..., Any]
    inject: tuple[str, ...] = ()
    optional_inject: tuple[str, ...] = ()
    config_schema: Any = None
    source: str = ""
    raw: Any = field(default=None, repr=False)


def resolve_plugin(spec: Any) -> ResolvedPlugin:
    if isinstance(spec, ResolvedPlugin):
        return spec
    if isinstance(spec, str):
        return _from_string(spec)
    if inspect.ismodule(spec):
        return _from_object(spec, spec, default_name=getattr(spec, "__name__", "plugin"))
    if inspect.isclass(spec):
        return _from_class(spec)
    return _from_object(spec, spec, default_name=getattr(spec, "name", ""))


def _from_string(spec: str) -> ResolvedPlugin:
    text = spec.strip()
    if not text:
        raise PluginRejected("KERNEL_PLUGIN_SPEC", "plugin spec must not be empty")
    target, _, attr = text.partition(":")
    attr = attr or DEFAULT_ATTR
    if target.endswith(".py") or "/" in target or "\\" in target:
        path = Path(target).expanduser()
        if not path.is_file():
            raise PluginRejected("KERNEL_PLUGIN_NOT_FOUND", f"plugin file not found: {target}", plugin=target)
        module_name = f"_eclipse_plugin_{abs(hash(path.resolve()))}"
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        if module_spec is None or module_spec.loader is None:
            raise PluginRejected("KERNEL_PLUGIN_IMPORT", f"cannot import {target}", plugin=target)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        try:
            module_spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - import failure is a named rejection
            sys.modules.pop(module_name, None)
            raise PluginRejected(
                "KERNEL_PLUGIN_IMPORT", f"importing {target} failed: {type(exc).__name__}: {exc}",
                plugin=target,
            ) from exc
        return _from_object(getattr(module, attr, None), module, default_name=path.stem, source=target)
    try:
        module = importlib.import_module(target)
    except Exception as exc:  # noqa: BLE001
        raise PluginRejected(
            "KERNEL_PLUGIN_IMPORT", f"importing {target} failed: {type(exc).__name__}: {exc}",
            plugin=target,
        ) from exc
    return _from_object(getattr(module, attr, None), module, default_name=target.rsplit(".", 1)[-1], source=text)


def _from_class(cls: type) -> ResolvedPlugin:
    # A class plugin is instantiated only when it needs no constructor arguments;
    # otherwise its `apply` is treated as a static/class method.
    instance: Any = cls
    try:
        signature = inspect.signature(cls.__init__)
        required = [
            p for p in signature.parameters.values()
            if p.name != "self" and p.default is p.empty
            and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        if not required:
            instance = cls()
    except (TypeError, ValueError):
        pass
    return _from_object(instance, cls, default_name=getattr(cls, "name", cls.__name__))


def _from_object(obj: Any, owner: Any, *, default_name: str, source: str = "") -> ResolvedPlugin:
    if callable(getattr(obj, "apply", None)):
        target = obj.apply
    elif callable(obj):
        target = obj
    else:
        target = getattr(owner, "apply", None)
    if target is None:
        raise PluginRejected(
            "KERNEL_PLUGIN_APPLY",
            f"plugin {default_name!r} has no apply(ctx) entrypoint",
            plugin=default_name,
        )
    if not callable(target):
        raise PluginRejected(
            "KERNEL_PLUGIN_APPLY", f"plugin {default_name!r} apply is not callable", plugin=default_name,
        )
    name = str(
        getattr(owner, "name", None)
        or getattr(obj, "name", None)
        or default_name
        or getattr(target, "__name__", "plugin")
    )
    inject = _tuple(getattr(owner, "inject", None) or getattr(obj, "inject", None))
    optional = _tuple(getattr(owner, "optional_inject", None) or getattr(obj, "optional_inject", None))
    schema = getattr(owner, "Config", None) or getattr(obj, "Config", None)
    return ResolvedPlugin(
        name=name,
        apply=target,
        inject=inject,
        optional_inject=optional,
        config_schema=schema,
        source=source or str(getattr(owner, "__module__", "")),
        raw=obj,
    )


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)
