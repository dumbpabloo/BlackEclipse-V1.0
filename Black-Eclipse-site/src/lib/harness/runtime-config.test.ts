import { describe, it, expect } from "vitest";
import { execFile } from "child_process";
import { promisify } from "util";
import fs from "fs";
import os from "os";
import path from "path";
import {
  PYTHON_CHART_PACKAGES,
  PYTHON_RUNTIME_DEPS,
  PYTHON_RUNTIME_PACKAGES,
  chartEngineReadiness,
  chartEngineReadinessFromHealth,
} from "@/lib/harness/runtime-config";
import { findProjectRoot, findPython } from "@/lib/scraper/fetchers/python";

const execFileAsync = promisify(execFile);

// ── The runtime marker contract ─────────────────────────────────────────────
//
// Regression: a partially installed system Python could import `langgraph` but
// had no provider package. The launcher selected it over the complete vendored
// interpreter, and every model call failed with
// "No module named 'langchain_openai'". The marker must therefore name a
// provider integration alongside the graph engine.
describe("runtime install marker", () => {
  it("requires a provider package, not just langgraph", () => {
    const markers = PYTHON_RUNTIME_DEPS.split(",").map((m) => m.trim());
    expect(markers).toContain("langgraph");
    expect(markers).toContain("langchain_openai");
  });
});

// ── The chart engine's dependency contract ──────────────────────────────────
//
// This is the regression test for a machine where the agent runtime was
// installed and healthy but `matplotlib` was not: every chat chart 503'd, and
// nothing in the build caught it. Two facts must stay true:
//
//   1. every third-party module `python/harness/charts/*.py` imports is listed
//      in the install list, so no future chart dependency can be silently
//      omitted from `npm run setup:python`;
//   2. the interpreter the runtime actually selects can import the whole
//      chart stack — the difference between "installed somewhere" and "usable
//      where the engine runs".

// Python's stdlib roots reached from the charts package. Deliberately explicit
// (rather than trying to know every stdlib name): a new stdlib import is a
// harmless test edit, a new THIRD-PARTY import without an install entry is the
// failure this exists to catch.
const PYTHON_STDLIB = new Set([
  "__future__",
  "dataclasses",
  "hashlib",
  "importlib",
  "io",
  "json",
  "math",
  "os",
  "sys",
  "typing",
  "warnings",
]);

function importRoots(file: string): string[] {
  const source = fs.readFileSync(file, "utf8");
  const roots = new Set<string>();
  // `from x.y import z` and `import x.y as z` / `import x, y`.
  const fromRe = /^\s*from\s+([A-Za-z_][\w.]*)\s+import\b/gm;
  const importRe = /^\s*import\s+([A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*)/gm;
  for (const match of source.matchAll(fromRe)) {
    roots.add(match[1].split(".")[0]);
  }
  for (const match of source.matchAll(importRe)) {
    for (const part of match[1].split(",")) {
      const name = part.trim().split(/\s+as\s+/)[0].split(".")[0];
      if (name) roots.add(name);
    }
  }
  return [...roots];
}

describe("chart engine dependency contract", () => {
  it("lists every chart package in the Python install set", () => {
    const install = new Set<string>(PYTHON_RUNTIME_PACKAGES);
    const missing = PYTHON_CHART_PACKAGES.filter((name) => !install.has(name));
    expect(missing, `add to PYTHON_RUNTIME_PACKAGES: ${missing.join(", ")}`).toEqual([]);
  });

  it("lists every third-party module the charts package imports", () => {
    const chartsDir = path.join(process.cwd(), "python", "harness", "charts");
    const files = fs
      .readdirSync(chartsDir)
      .filter((name) => name.endsWith(".py") && name !== "__init__.py");

    const thirdParty = new Set<string>();
    for (const file of files) {
      for (const root of importRoots(path.join(chartsDir, file))) {
        if (!PYTHON_STDLIB.has(root)) thirdParty.add(root);
      }
    }

    // The scan must actually have found the drawing libraries; a silently empty
    // result would make the assertion below vacuous.
    expect(thirdParty.has("matplotlib")).toBe(true);
    expect(thirdParty.has("plotly")).toBe(true);

    const install = new Set<string>(PYTHON_RUNTIME_PACKAGES);
    const missing = [...thirdParty].filter((name) => !install.has(name));
    expect(
      missing,
      `python/harness/charts imports ${missing.join(", ")} but PYTHON_RUNTIME_PACKAGES does not install it`,
    ).toEqual([]);
  });

  it("reports a chart engine as ready only when every package is present", () => {
    expect(chartEngineReadiness("/usr/bin/python", [...PYTHON_CHART_PACKAGES])).toMatchObject({
      ready: true,
      missing: [],
    });
    const partial = chartEngineReadiness("/usr/bin/python", ["numpy", "plotly"]);
    expect(partial.ready).toBe(false);
    expect(partial.missing).toContain("matplotlib");
    expect(chartEngineReadiness(null, [...PYTHON_CHART_PACKAGES]).ready).toBe(false);
  });

  // The runtime that draws is the authority on drawing. This decision used to
  // be made by probing a SEPARATELY resolved interpreter, which disagreed with
  // the running runtime and refused every chart with
  // `CHART_RENDER_UNAVAILABLE` while the runtime could draw perfectly.
  it("reads the runtime's own /charts/health document", () => {
    const healthy = chartEngineReadinessFromHealth({
      status: "OK",
      python: "C:/venv/python.exe",
      matplotlib: true,
      plotly: true,
      numpy: true,
      canDraw: true,
    });
    expect(healthy).toEqual({ ready: true, python: "C:/venv/python.exe", missing: [] });

    const shortOfPlotly = chartEngineReadinessFromHealth({
      status: "OK",
      python: "C:/venv/python.exe",
      matplotlib: true,
      plotly: false,
      numpy: true,
    });
    expect(shortOfPlotly?.ready).toBe(false);
    expect(shortOfPlotly?.missing).toEqual(["plotly"]);

    expect(chartEngineReadinessFromHealth({ status: "STARTING" })).toBeNull();
    expect(chartEngineReadinessFromHealth(null)).toBeNull();
    expect(chartEngineReadinessFromHealth([1, 2, 3])).toBeNull();
  });

  // The environment assertion. It probes the interpreter that can actually
  // boot the runtime (`langgraph`), so it is skipped on a machine/CI job with no
  // agent stack — but a machine that HAS the runtime and lacks the chart stack
  // fails here, which is exactly the bug this prevents. The Python CI job also
  // runs the same import through `scripts/test-python.js`.
  it(
    "the interpreter the runtime selects can import the chart stack",
    async () => {
      const py = await findPython(PYTHON_RUNTIME_DEPS);
      if (!py) return;
      const code = `import ${PYTHON_CHART_PACKAGES.join(", ")}`;
      await expect(
        execFileAsync(py.cmd, ["-c", code], { timeout: 90_000, windowsHide: true }),
      ).resolves.toBeTruthy();
    },
    120_000,
  );
});

describe("project-root discovery", () => {
  it("finds the checkout from a nested pinned-release cwd", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "be-root-"));
    fs.mkdirSync(path.join(root, "python", "harness"), { recursive: true });
    fs.writeFileSync(path.join(root, "python", "harness", "main.py"), "");
    const nested = path.join(root, "releases", "stable", "build", ".next", "server");
    fs.mkdirSync(nested, { recursive: true });
    expect(findProjectRoot(nested)).toBe(root);
  });
});
