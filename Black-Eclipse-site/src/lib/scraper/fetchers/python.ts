// Cross-platform Python resolution for the bridge fetchers.
//
// Windows ships `python` (and `py`); macOS/Linux ship `python3` (and
// sometimes `python` if python-is-python3 is installed). Spawning the wrong
// name throws ENOENT, so every bridge call must go through this resolver.

import { execFile } from "child_process";
import { existsSync } from "fs";
import path from "path";
import { promisify } from "util";

const execFileAsync = promisify(execFile);

type PythonInfo = { cmd: string } | null;

const CANDIDATES = process.platform === "win32" ? ["python", "python3", "py"] : ["python3", "python"];

// Vendored runtime provisioned by scripts/setup-embedded-python.js (Windows)
// and scripts/setup-portable-python.js (macOS/Linux). When present it is the
// preferred interpreter, so agent features work without a system Python.
//
// This used to be Windows-only, which meant a packaged macOS/Linux install
// looked for a SYSTEM python3 with `langgraph` and never found it: the release
// smoke reported "Python with agent dependencies not found" while the app's own
// interpreter sat unused in scripts/vendor/python/bin/python3.
/**
 * The checkout/payload root, independent of the process cwd.
 *
 * A pinned desktop release runs `next start` (or the standalone server) with a
 * cwd that can be the release directory, not the repo: resolving the vendored
 * interpreter from `process.cwd()` then reports "Python with agent dependencies
 * not found" even though `scripts/vendor/python` exists one level up. Walk the
 * cwd and this module's own directory up to a root that contains either the
 * vendored interpreter or `python/harness/main.py`.
 */
export function findProjectRoot(start = process.cwd()): string {
  const anchors = [start, __dirname];
  for (const anchor of anchors) {
    let current = path.resolve(anchor);
    for (let depth = 0; depth < 12; depth += 1) {
      const vendor = path.join(current, "scripts", "vendor", "python");
      const harness = path.join(current, "python", "harness", "main.py");
      if (existsSync(harness) || existsSync(vendor)) return current;
      const parent = path.dirname(current);
      if (parent === current) break;
      current = parent;
    }
  }
  return start;
}

const VENDOR_ROOT = findProjectRoot();
const VENDOR_INTERPRETER = path.join(
  VENDOR_ROOT,
  "scripts",
  "vendor",
  "python",
  ...(process.platform === "win32" ? ["python.exe"] : ["bin", "python3"]),
);

async function canRun(cmd: string, mod?: string): Promise<boolean> {
  try {
    const args = mod ? ["-c", `import ${mod}`] : ["--version"];
    await execFileAsync(cmd, args, { timeout: 10_000, windowsHide: true });
    return true;
  } catch {
    return false;
  }
}

/**
 * Find a usable Python interpreter, optionally one that has `mod` importable.
 * Prefers the vendored embedded Python when present, then a system interpreter.
 * Cached per module (a missing interpreter stays cached; a module check is
 * re-probed after 5 minutes like the yfinance availability cache).
 */
export async function findPython(mod?: string): Promise<PythonInfo> {
  const key = mod ?? "__interpreter__";
  const store = globalThis as unknown as {
    __pyCache?: Map<string, { value: PythonInfo; at: number }>;
  };
  if (!store.__pyCache) store.__pyCache = new Map();
  const hit = store.__pyCache.get(key);
  const ttl = mod ? 5 * 60 * 1000 : Infinity;
  if (hit && Date.now() - hit.at < ttl) return hit.value;

  // Vendored embedded Python is the primary choice on Windows — but only when
  // it actually exists on disk. The path is built at module load, and reporting
  // a missing interpreter as "found" made every caller spawn a file that is not
  // there: the chart-engine probe then read that failed spawn as "this Python
  // has no matplotlib" and refused every chart.
  if (
    VENDOR_INTERPRETER &&
    existsSync(VENDOR_INTERPRETER) &&
    (mod ? await canRun(VENDOR_INTERPRETER, mod) : true)
  ) {
    const result: PythonInfo = { cmd: VENDOR_INTERPRETER };
    store.__pyCache.set(key, { value: result, at: Date.now() });
    return result;
  }

  let result: PythonInfo = null;
  for (const cmd of CANDIDATES) {
    if (await canRun(cmd, mod)) {
      result = { cmd };
      break;
    }
  }
  store.__pyCache.set(key, { value: result, at: Date.now() });
  return result;
}

/** Run a Python bridge script with the resolved interpreter. */
export async function runPythonBridge(
  scriptPath: string,
  args: string[],
  opts: { timeout: number; maxBuffer?: number; mod?: string }
): Promise<{ stdout: string }> {
  const py = await findPython(opts.mod);
  if (!py) {
    throw new Error(
      `Python not found${opts.mod ? ` (with "${opts.mod}" installed)` : ""}. Install Python 3 and run: pip install yfinance scrapling`
    );
  }
  return execFileAsync(py.cmd, [scriptPath, ...args], {
    timeout: opts.timeout,
    maxBuffer: opts.maxBuffer ?? 8 * 1024 * 1024,
    windowsHide: true,
  });
}