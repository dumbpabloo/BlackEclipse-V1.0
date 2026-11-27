#!/usr/bin/env node
// Prune the agent-memory event store (dry run by default).
//
//   npm run memory:prune                 # report what would be pruned (30 days)
//   npm run memory:prune -- --days 7 --apply
//   npm run memory:prune -- --days 30 --apply --vacuum
//
// Uses the SAME interpreter the runtime uses, and never guesses the DB path:
// `harness.retention` resolves the runtime's default store (or `--db`).
const { spawnSync } = require("child_process");
const path = require("path");
const { root, pythonDir, resolvePython, pythonPathEnv } = require("./python-runner");

const py = resolvePython("import harness") || resolvePython();
if (!py) {
  console.error("[memory:prune] Python runtime not found. Run: npm run setup:python");
  process.exit(1);
}

// The vendored interpreter ships a `python312._pth` that disables PYTHONPATH
// and cwd imports, so `-m harness.retention` cannot resolve. Seed sys.path
// explicitly (the same reason the test runner sets PYTHONPATH for pytest).
const bootstrap = [
  "import sys",
  `sys.path.insert(0, r"${pythonDir}")`,
  "from harness.retention import main",
  "raise SystemExit(main())",
].join("; ");
const args = ["-c", bootstrap, ...process.argv.slice(2)];
const env = {
  ...process.env,
  PYTHONPATH: pythonPathEnv(),
};
const result = spawnSync(py, args, {
  cwd: path.join(root, "python"),
  stdio: "inherit",
  windowsHide: true,
  env,
});
if (result.error) {
  console.error(`[memory:prune] failed to start Python: ${result.error.message}`);
  process.exit(1);
}
process.exit(result.status ?? 1);
