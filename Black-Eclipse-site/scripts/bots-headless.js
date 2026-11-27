// Run the Python agent runtime (and therefore the bot scheduler) with no
// Electron/Next process. This is the always-on host path: put it on a VPS or a
// spare machine and scheduled bots keep running while the desktop is closed.
//
// It binds loopback by default. A non-loopback bind is refused by the runtime
// unless the operator secret is configured (see harness/main.py); this script
// does not weaken that guard.
const { spawn } = require("child_process");
const { pythonDir, resolvePython, pythonPathEnv } = require("./python-runner");

const py = resolvePython("import langgraph, uvicorn, fastapi");
if (!py) {
  console.error("[bots] Python agent stack is not installed. Run: npm run setup:python");
  process.exit(1);
}

const host = process.env.BOTS_HOST || "127.0.0.1";
const port = Number(process.env.BOTS_PORT || 8765);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  console.error("[bots] BOTS_PORT must be a valid TCP port.");
  process.exit(1);
}

const launcher = [
  "import sys",
  `sys.path.insert(0, ${JSON.stringify(pythonDir)})`,
  "from harness.main import main",
  `main(host=${JSON.stringify(host)}, port=${port})`,
].join("\n");

console.log(`[bots] headless runtime on http://${host}:${port}`);
console.log("[bots] scheduled bots run here; there is no desktop UI in this process.");
const child = spawn(py, ["-c", launcher], {
  cwd: process.cwd(),
  stdio: "inherit",
  env: { ...process.env, PYTHONPATH: pythonPathEnv() },
});
const stop = (signal) => {
  if (!child.killed) child.kill(signal);
};
process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
child.on("exit", (code) => process.exit(code ?? 0));
