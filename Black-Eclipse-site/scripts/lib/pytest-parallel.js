// Run the Python test suite as N independent pytest processes over disjoint
// chunks of test files.
//
// WHY this exists: the suite is 1017 test files and one serial pytest process.
// Measured on a dev machine the full run is ~953s, of which ~607s is collection
// (importing every test module) and the rest is dominated by a few slow files.
// Because that cost is per-file, splitting the FILE LIST across processes cuts
// wall-clock almost linearly - each worker only pays to import the files it was
// given - while `pytest-xdist` would still collect the whole session per worker.
//
// Chunks are taken dynamically (a worker asks for the next chunk when it
// finishes), so one slow chunk does not stall a whole shard.
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

/**
 * @param {object} opts
 * @param {string} opts.py       interpreter
 * @param {string} opts.cwd      working directory for pytest
 * @param {object|function(number):object} opts.env  child environment (or a
 *   per-worker factory, so parallel workers never share one test SQLite file)
 * @param {string[]} opts.files  test files, relative to cwd
 * @param {number} opts.workers  parallel processes
 * @param {string[]} opts.args   extra pytest arguments (empty for the gate)
 * @returns {Promise<number>} exit code
 */
async function runParallel({ py, cwd, env, files, workers, args = [] }) {
  const count = Math.max(1, Math.min(workers, files.length));
  // More chunks than workers so the last stretch is not one long straggler.
  const chunkCount = Math.min(files.length, count * 4);
  const chunks = Array.from({ length: chunkCount }, () => []);
  files.forEach((file, index) => chunks[index % chunkCount].push(file));

  const logDir = fs.mkdtempSync(path.join(os.tmpdir(), "be-pytest-shard-"));
  const logs = [];

  const runChunk = (worker, chunk, index) =>
    new Promise((resolve) => {
      const log = path.join(logDir, `worker-${worker}-${index}.log`);
      logs.push(log);
      const out = fs.openSync(log, "w");
      // `env` may be a factory: concurrent workers must not share one SQLite
      // file, or their startup migrations race (duplicate column) and a chunk
      // fails during COLLECTION before a single test runs.
      const chunkEnv = typeof env === "function" ? env(worker) : env;
      const child = spawn(py, ["-m", "pytest", ...args, ...chunk], { cwd, env: chunkEnv, stdio: ["ignore", out, out] });
      child.on("close", (code) => {
        fs.closeSync(out);
        resolve(code ?? 1);
      });
      child.on("error", () => {
        try { fs.closeSync(out); } catch {}
        resolve(1);
      });
    });

  let next = 0;
  let exitCode = 0;

  const worker = async (id) => {
    for (;;) {
      const index = next++;
      if (index >= chunks.length) return;
      const code = await runChunk(id, chunks[index], index);
      if (code !== 0) exitCode = code;
    }
  };

  const started = Date.now();
  await Promise.all(Array.from({ length: count }, (_, id) => worker(id)));

  // Replay each worker's log in full: pytest's failure blocks are the point of
  // running the suite, and they are per-worker. Interleaving live output would
  // make them unreadable, so the logs are buffered and printed in order.
  for (const log of logs) {
    const text = fs.readFileSync(log, "utf8");
    process.stdout.write(text);
  }
  fs.rmSync(logDir, { recursive: true, force: true });

  const seconds = Math.round((Date.now() - started) / 1000);
  console.log(`[test] python: ${files.length} files across ${count} workers in ${seconds}s`);
  return exitCode;
}

module.exports = { runParallel };
