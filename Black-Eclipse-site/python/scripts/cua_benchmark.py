#!/usr/bin/env python
"""Run the local Windows Computer Use capability suite and print pass@1.

Usage (from Black-Eclipse-site/python):
    python scripts/cua_benchmark.py
    python scripts/cua_benchmark.py --min 0.9

This drives a REAL Win32 window the benchmark owns, with the real Windows
driver. It measures the ENGINE (perception, grounding, input, verification, wait,
clipboard, drag, learned interfaces), not model intelligence — do not present it
as an OSWorld/agent score.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.computer.config import computer_config  # noqa: E402
from harness.computer.eval.windows_suite import LocalWindowsSuite  # noqa: E402
from harness.computer.eval.suite import run_suite  # noqa: E402
from harness.computer.manager import ComputerManager  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target", default=str(ROOT / "tests" / "computer_stress_target.py"),
        help="the owned Win32 target script",
    )
    parser.add_argument("--min", type=float, default=0.75, help="fail below this pass@1")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("UNAVAILABLE: the local Windows suite requires Windows.")
        return 2
    manager = ComputerManager(config=computer_config(refresh=True))
    await manager.start()
    suite = LocalWindowsSuite(manager, Path(args.target), Path(tempfile.mkdtemp(prefix="cua-bench-")))
    try:
        await suite.launch()
        report = await run_suite(suite.tasks(), suite="windows-core")
        print(report.report())
        return 0 if report.pass_at_1 >= args.min else 1
    finally:
        await suite.close()
        await manager.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
