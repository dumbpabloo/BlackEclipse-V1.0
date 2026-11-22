"""`eclipse kernel` - inspect and compare compositions without mounting them.

Commands:

    eclipse kernel dump   --profile web [--json] [--patch FILE ...]
    eclipse kernel diff   --profile web --patch FILE
    eclipse kernel doctor --profile web

Nothing here imports a plugin or mutates a profile. `dump` is the artifact a
reviewer can diff before boot; `doctor` explains why a row would not contribute.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .dump import diagnose, diff, render, render_json
from .profile import load_profile


def _profile_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "profile_dir", ""):
        return Path(args.profile_dir).expanduser()
    home = Path(getattr(args, "home", "") or (Path.home() / ".black-eclipse"))
    return home / "profiles" / str(args.profile)


def _load(args: argparse.Namespace, patches: Optional[list[str]] = None):
    chosen = list(getattr(args, "patch", []) or []) if patches is None else list(patches)
    return load_profile(
        _profile_dir(args),
        bundle_paths=getattr(args, "bundle_path", []) or [],
        extra_patches=chosen,
    )


def run_cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="eclipse kernel", description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    for name, help_text in (
        ("dump", "print the effective composition"),
        ("diff", "compare the profile with one overlay applied"),
        ("doctor", "report composition problems"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        group = cmd.add_mutually_exclusive_group(required=True)
        group.add_argument("--profile", default="", help="profile name under ~/.black-eclipse/profiles")
        group.add_argument("--profile-dir", default="", help="explicit profile directory")
        cmd.add_argument("--home", default="", help="Harness home (defaults to ~/.black-eclipse)")
        cmd.add_argument("--bundle-path", action="append", default=[], help="extra bundle search root")
        cmd.add_argument("--patch", action="append", default=[], help="ordered patch overlay")
        if name == "dump":
            cmd.add_argument("--json", action="store_true", help="emit JSON instead of YAML")

    args = parser.parse_args(argv)
    try:
        if args.action == "diff":
            if not args.patch:
                parser.error("diff needs at least one --patch overlay")
            base = _load(args, patches=[])
            after = _load(args)
            print(json.dumps(diff(base.composition, after.composition), indent=2, sort_keys=True))
            return 0
        loaded = _load(args)
        if args.action == "dump":
            print(render_json(loaded.composition) if args.json else render(loaded.composition))
            return 0
        problems = diagnose(loaded.composition)
        if not problems:
            print(f"kernel: {loaded.name}: {len(loaded.composition.enabled())} enabled row(s), no problems")
            return 0
        for item in problems:
            print(f"{item.get('code')}: {item.get('message')} [{item.get('source', '')}]")
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI reports a named failure
        code = getattr(exc, "code", "KERNEL_CLI_FAILED")
        print(f"{code}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_cli())
