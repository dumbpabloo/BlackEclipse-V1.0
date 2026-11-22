"""The Eclipse plugin kernel.

Public surface:

    from harness.kernel import Kernel, Context, Supervisor, load_profile

The kernel is additive. With ``ECLIPSE_KERNEL`` unset/off the existing boot path
is unchanged; this package is exercised by its own tests and by the
``eclipse kernel dump`` command until a phase wires it into service boot.
"""

from .composition import Composition, Layer, Row, compose
from .context import Context, Kernel
from .errors import (
    CompositionError,
    KernelError,
    PluginRejected,
    ServiceConflict,
    ServiceUnavailable,
    SupervisorError,
)
from .events import MODES, EventBus
from .fiber import Fiber, FiberState
from .plugin import ResolvedPlugin, resolve_plugin
from .profile import BundleRef, Profile, load_profile
from .scope import ScopedRegistry
from .supervisor import Supervisor

__all__ = [
    "BundleRef",
    "Composition",
    "CompositionError",
    "Context",
    "EventBus",
    "Fiber",
    "FiberState",
    "Kernel",
    "KernelError",
    "Layer",
    "MODES",
    "PluginRejected",
    "Profile",
    "ResolvedPlugin",
    "Row",
    "ScopedRegistry",
    "ServiceConflict",
    "ServiceUnavailable",
    "Supervisor",
    "SupervisorError",
    "compose",
    "load_profile",
    "resolve_plugin",
]
