// Run the Python harness test suite with the same interpreter the runtime uses.
// Skips (exit 0) when Python or the agent stack is absent, so `npm test` stays
// usable on a machine without the runtime — the TS suite still gates the app.
const { spawnSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { root, pythonDir, resolvePython, pythonPathEnv } = require("./python-runner");

// Same interpreter the runtime uses. The probe is the test suite's requirement:
// the agent stack AND pytest.
const py = resolvePython("import langgraph, pytest");
if (!py) {
  console.log("[test] Python agent stack not installed — skipping Python tests.");
  console.log("[test] Install it with: npm run setup:python");
  process.exit(0);
}

// The chart engine's dependencies are part of the runtime contract, not an
// optional extra: a runtime that boots without matplotlib cannot draw a single
// chart, and that used to surface only as a 503 in the chat. Fail the build
// here instead, naming the interpreter that is short a package.
const chartProbe = spawnSync(py, ["-c", "import matplotlib, plotly, numpy, pydantic"], {
  stdio: "inherit",
});
if (chartProbe.status !== 0) {
  console.error(
    `[test] CHART ENGINE UNAVAILABLE — ${py} cannot import matplotlib/plotly/numpy/pydantic.`,
  );
  console.error("       Install the stack, then re-run:");
  console.error(`         "${py}" -m pip install -r python/analysis-requirements.txt`);
  process.exit(1);
}

// `node scripts/test-python.js smoke` runs the OPERATOR-TRIGGERED live smoke
// checks, which contact real external providers. They are deliberately separate
// from the default offline suite: never part of `npm test`.
// The vendored interpreter ships a `python312._pth` that disables implicit
// sys.path[0] insertion, so `-m harness.*` cannot resolve the package from cwd
// alone. Set PYTHONPATH explicitly for every child (pytest and the smoke runner)
// so the vendored and system interpreters behave identically.
// Isolate the suite from the developer's real memory database. `eclipse-memory.db`
// carries installed skill versions; reusing it across runs made `test_skills_api`
// fail on a stale content hash. Tests are expected to get a temp file (see
// `harness/eclipse_memory/config.py`); this runner makes that true for the whole
// suite, not just the modules that call `setdefault` themselves.
const isolatedMemoryDir = fs.mkdtempSync(path.join(os.tmpdir(), "be-pytest-"));
const childEnv = {
  ...process.env,
  // Both stores are per-run temp files. The harness service reads these paths at
  // import time; modules that call `setdefault` then agree with the booted
  // service instead of racing it with their own temp path.
  AGENT_MEMORY_DB_PATH: path.join(isolatedMemoryDir, "agent-memory.db"),
  ECLIPSE_MEMORY_DB_PATH: path.join(isolatedMemoryDir, "eclipse-memory.db"),
  // Do not open/warm the developer's real code index during the test suite: the
  // boot warm-up is an operational affordance, not part of what is under test.
  BLACK_ECLIPSE_CODE_INTEL_WARM: "0",
  PYTHONPATH: pythonPathEnv(),
};

if (process.argv.includes("smoke")) {
  console.log("[test] Running live network smoke checks (contacts real providers)…");
  // The vendored interpreter ships a `python312._pth` that ignores PYTHONPATH and
  // cwd, so `-m harness.*` cannot resolve the package. Inject the path explicitly
  // and call the runner, which works identically for vendored and system pythons.
  const launcher = [
    "import sys",
    `sys.path.insert(0, ${JSON.stringify(pythonDir)})`,
    "from harness.research.smoke import main",
    "sys.exit(main())",
  ].join("; ");
  const smoke = spawnSync(py, ["-c", launcher], {
    stdio: "inherit",
    cwd: pythonDir,
    env: childEnv,
  });
  process.exit(smoke.status ?? 1);
}

// Any extra arguments are passed to pytest, so a slice can be run on its own:
//   node scripts/test-python.js python/tests/test_rsl_loop.py
// The full suite is `node scripts/test-python.js` (no arguments).
const extra = process.argv.slice(2).filter((arg) => arg !== "smoke" && arg !== "--serial");

// The full suite is split across independent pytest processes. The cost is
// per-file (import + collection), so this is close to linear in wall-clock;
// `pytest-xdist` would re-collect the whole session in every worker instead.
// Slices (an explicit file list) stay serial: the caller already narrowed them,
// and `test:rsl` runs single files where spawn overhead would dominate.
// Override the fan-out with BE_TEST_WORKERS, force serial with --serial.
if (extra.length === 0 && !process.argv.includes("--serial")) {
  const { runParallel } = require("./lib/pytest-parallel");
  const requested = Number(process.env.BE_TEST_WORKERS);
  // Default is SERIAL, measured rather than assumed: on a 12-core / 16 GB
  // Windows box with the desktop app resident, four workers took 1333s against
  // 953s serial. Each worker holds the full harness import graph, so the
  // fan-out only pays off when there is RAM to hold several of them. Set
  // BE_TEST_WORKERS to measure your own machine; it is opt-in, not a default.
  const workers = Number.isFinite(requested) && requested > 1 ? requested : 1;

  const testRoot = path.join(pythonDir, "tests");
  const files = fs
    .readdirSync(testRoot, { recursive: true })
    .map((entry) => String(entry))
    .filter((entry) => /(^|[\\/])test_.*\.py$/.test(entry))
    .map((entry) => path.relative(root, path.join(testRoot, entry)));

  console.log(`[test] python: ${files.length} files, ${workers} workers`);
  // One SQLite pair PER WORKER. Sharing one file let two processes run startup
  // migrations at the same time (`ALTER TABLE` duplicate column) and fail a
  // chunk during collection.
  const envForWorker = (worker) => ({
    ...childEnv,
    AGENT_MEMORY_DB_PATH: path.join(isolatedMemoryDir, `agent-memory-${worker}.db`),
    ECLIPSE_MEMORY_DB_PATH: path.join(isolatedMemoryDir, `eclipse-memory-${worker}.db`),
  });
  runParallel({ py, cwd: root, env: envForWorker, files, workers })
    .then((code) => process.exit(code))
    .catch(() => process.exit(1));
} else {
  const targets = extra.length > 0 ? extra : [pythonDir];
  const result = spawnSync(py, ["-m", "pytest", ...targets], { stdio: "inherit", cwd: root, env: childEnv });
  process.exit(result.status ?? 1);
}
