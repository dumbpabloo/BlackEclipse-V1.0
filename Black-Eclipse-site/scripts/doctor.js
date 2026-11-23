// Self-healing environment check. Runs before dev/start: verifies Node,
// deps, Prisma client, DB, and Python bridge — auto-fixing what it can.
// Silent when everything is fine; prints [setup] lines when it fixes something.
const { execSync, spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");
const { consumeFactoryReset } = require("./lib/reset");

const root = path.join(__dirname, "..");

// `BE_DATA_DIR` moves the personal databases onto an encrypted volume
// (scripts/setup-data-vault.ps1). Every path below — and prisma.config.ts,
// src/lib/db.ts and the Python stores — must agree on it.
function readDotEnvValue(key) {
  try {
    const text = fs.readFileSync(path.join(root, ".env"), "utf8");
    for (const line of text.split(/\r?\n/)) {
      const m = /^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$/.exec(line);
      if (m && m[1] === key) return m[2].replace(/^["']|["']$/g, "");
    }
  } catch {
    /* no .env yet */
  }
  return undefined;
}

const dataDir = process.env.BE_DATA_DIR
  ? path.resolve(process.env.BE_DATA_DIR)
  : root;
const jddsPath = path.join(dataDir, "jdds.db");

// Propagate the resolved vault paths to the child processes doctor starts
// (db-guard in particular) BEFORE they run. Doctor is not Next and does not load
// `.env` automatically; without this, db-guard would look at <repo>/jdds.db,
// find nothing, and create an EMPTY database outside the encrypted volume.
{
  const envDataDir = process.env.BE_DATA_DIR || readDotEnvValue("BE_DATA_DIR");
  if (envDataDir) process.env.BE_DATA_DIR = path.resolve(envDataDir);
  if (dataDir !== root) process.env.BE_DB_PATH = jddsPath;
}

// ── Encrypted-vault guard ───────────────────────────────────────────────────
// A locked or detached vault looks exactly like "no database yet" to every
// later step, and `prisma db push` would happily create an EMPTY one on the
// wrong disk. Fail closed instead: refuse to start until the volume is mounted.
{
  const vaultPath = process.env.BE_VAULT_PATH || readDotEnvValue("BE_VAULT_PATH");
  const envDataDir = process.env.BE_DATA_DIR || readDotEnvValue("BE_DATA_DIR");
  const vaultRoot = vaultPath ? path.parse(path.resolve(vaultPath)).root : null;
  if (vaultRoot && !fs.existsSync(vaultRoot)) {
    console.error(
      `[setup] Encrypted data vault is not mounted (${vaultRoot} missing).\n` +
        "  Mount/unlock your vault volume, then start again. Nothing was created or moved.",
    );
    process.exit(1);
  }
  if (envDataDir && !fs.existsSync(path.resolve(envDataDir))) {
    console.error(
      `[setup] BE_DATA_DIR does not exist: ${path.resolve(envDataDir)}\n` +
        "  Unlock the vault volume, or unset BE_DATA_DIR in .env to use the repo folder.",
    );
    process.exit(1);
  }
}
// ── Pending factory reset ───────────────────────────────────────────────────
// The Settings danger zone writes a marker instead of deleting live SQLite from
// the web process. Consume it here, BEFORE the DB guard opens/creates jdds.db,
// so the app boots as a genuinely new install. The vault must be mounted first
// (the guard above), otherwise we would wipe the wrong disk.
{
  try {
    const result = consumeFactoryReset({ log: (m) => console.log(m) });
    if (result) console.log("[setup] factory reset consumed; continuing with a fresh install.");
  } catch (err) {
    console.error(`[setup] factory reset failed: ${err && err.message ? err.message : err}`);
    process.exit(1);
  }
}

let fixed = 0;

function run(cmd, opts = {}) {
  // Always execute from the site root. `black eclipse` can be launched from any
  // directory (the global npm link), so without an explicit cwd a repair such as
  // `npm install` / `npx prisma generate` would run in the caller's folder and
  // fail or write outside the project.
  return spawnSync(cmd, { shell: true, cwd: root, stdio: "pipe", encoding: "utf8", windowsHide: true, ...opts });
}

function fix(label, cmd) {
  console.log(`[setup] ${label} missing — fixing (${cmd})...`);
  const r = run(cmd, { stdio: ["ignore", "ignore", "inherit"] });
  if (r.status !== 0) {
    console.error(`[setup] FAILED: ${label}. Run manually:\n  ${cmd}`);
    process.exit(1);
  }
  fixed++;
}

// 1. Node version (cannot auto-fix)
const major = Number(process.versions.node.split(".")[0]);
if (major < 20) {
  console.error(`[setup] Node.js v${process.versions.node} is too old — need v20+. Install from https://nodejs.org`);
  process.exit(1);
}

// 2. Dependencies
if (!fs.existsSync(path.join(root, "node_modules", ".package-lock.json"))) {
  fix("Dependencies", "npm install");
}

// 3. Prisma client
if (!fs.existsSync(path.join(root, "src", "generated", "prisma", "client.js")) &&
    fs.readdirSync(path.join(root, "src", "generated", "prisma")).length === 0) {
  fix("Prisma client", "npx prisma generate");
}

// 3a. Native SQLite binding for the Prisma adapter. Prisma's
//     `@prisma/adapter-better-sqlite3` depends on `better-sqlite3@^12`, which npm
//     may install NESTED under the adapter even when a different top-level
//     version exists. That nested copy is the one the runtime actually loads, and
//     it must have its native `better_sqlite3.node`. A plain `require()` of the
//     top-level package passes even when the nested binding is missing, so probe
//     the exact path the adapter resolves and repair it when absent. (Without
//     this, the DB reads fail at runtime and the app reports "no LLM provider is
//     configured" — the vault lives in jdds.db.)
{
  const adapterNested = path.join(
    root, "node_modules", "@prisma", "adapter-better-sqlite3", "node_modules", "better-sqlite3"
  );
  if (fs.existsSync(adapterNested)) {
    let bindingOk = false;
    try {
      const Database = require(adapterNested);
      const db = new Database(jddsPath, { readonly: true });
      db.prepare("select 1").get();
      db.close();
      bindingOk = true;
    } catch {
      bindingOk = false;
    }
    if (!bindingOk) {
      const prebuild = path.join(root, "node_modules", "prebuild-install", "bin.js");
      if (fs.existsSync(prebuild)) {
        console.log("[setup] Prisma adapter's better-sqlite3 binding missing — repairing...");
        const r = spawnSync(process.execPath, [prebuild], { cwd: adapterNested, stdio: "pipe", encoding: "utf8", windowsHide: true });
        if (r.status === 0) {
          fixed++;
        } else {
          console.error(
            "[setup] Could not install the native SQLite binding for the Prisma adapter.\n" +
            "        Install a C++ build toolchain (or set a prebuilt binary) and run:\n" +
            `          cd "${adapterNested}" && node "${prebuild}"`
          );
          process.exit(1);
        }
      }
    }
  }
}

// 4. Database (committed seed should exist; create empty if clone was partial)
if (!fs.existsSync(jddsPath)) {
  fix("Database", "npx prisma db push");
}

// 4a. Schema drift — an existing jdds.db can predate models added since the
//     committed seed snapshot (e.g. the connector + Investor Eye tables). A
//     stale DB would boot but every query against a missing table would 500,
//     so detect the drift and apply the additive schema. Expected table names
//     are parsed from schema.prisma so this check never goes stale.
{
  const schemaPath = path.join(root, "prisma", "schema.prisma");
  const dbExists = fs.existsSync(jddsPath);
  if (dbExists && fs.existsSync(schemaPath)) {
    try {
      const Database = require(path.join(root, "node_modules", "better-sqlite3"));
      const schema = fs.readFileSync(schemaPath, "utf8");
      const expected = [...schema.matchAll(/^\s*model\s+(\w+)\s*\{/gm)].map((m) => m[1]);
      const db = new Database(jddsPath, { readonly: true });
      const present = new Set(
        db.prepare("select name from sqlite_master where type = 'table'").all().map((r) => r.name)
      );
      db.close();
      const missing = expected.filter((t) => !present.has(t));
      if (missing.length > 0) {
        console.log(`[setup] Database schema is stale — missing ${missing.length} table(s): ${missing.join(", ")}`);
        fix("Database schema", "npx prisma db push");
      }
    } catch (err) {
      // A locked/corrupt DB is handled by the integrity guard just below; a
      // drift check that cannot read the DB must never block boot.
      console.warn(`[setup] schema drift check skipped: ${err.message}`);
    }
  }
}

// 4b. Database integrity guard — hardens WAL/synchronous, detects corruption,
//     and auto-repairs from a healthy rolling backup or the committed seed
//     snapshot BEFORE the server starts (so no /api route can hit a corrupt DB).
{
  const dbGuard = path.join(__dirname, "db-guard.js");
  if (fs.existsSync(dbGuard)) {
    const r = spawnSync(process.execPath, [dbGuard, "check"], { stdio: "inherit", windowsHide: true });
    if (r.status !== 0) {
      console.error("[setup] Database integrity check FAILED — see db-guard output above.");
      process.exit(1);
    }
  } else {
    console.warn("[setup] db-guard.js not found — skipping DB integrity check.");
  }
}

// Desktop launches ask for a fast boot: everything above repairs what the site
// cannot start without. Optional runtime probes (Python packages, browser
// binary, sandbox stack) are skipped here; `black doctor` / postinstall still
// run the full check where the extra seconds do not delay opening the app.
if (process.env.BE_DESKTOP_FAST === "1") {
  console.log("[setup] fast desktop boot — skipping optional runtime probes");
  process.exit(0);
}

// 5. Python yfinance bridge (optional — live prices)
// 5a. On Windows, auto-provision the vendored embedded Python if the system
//     has no Python, so live prices work out of the box for new users.
const vendorScript = path.join(__dirname, "setup-embedded-python.js");
const vendorReady =
  process.platform === "win32" &&
  fs.existsSync(path.join(__dirname, "vendor", "python", "python.exe"));
if (process.platform === "win32" && !vendorReady && fs.existsSync(vendorScript)) {
  console.log("[setup] No Python found — provisioning embedded Python for live prices...");
  const v = spawnSync(process.execPath, [vendorScript], { stdio: "inherit", windowsHide: true });
  if (v.status !== 0) {
    console.error("[setup] Could not provision embedded Python (offline?). Live prices disabled.");
  }
}

const pyCmds = process.platform === "win32" ? ["python", "python3", "py"] : ["python3", "python"];
const vendoredPy = path.join(__dirname, "vendor", "python", "python.exe");
const pyCandidates = fs.existsSync(vendoredPy) ? [vendoredPy, ...pyCmds] : pyCmds;
const py = pyCandidates.find((c) => run(`"${c}" --version`).status === 0);
if (py) {
  if (run(`"${py}" -c "import yfinance"`, { timeout: 15000 }).status !== 0) {
    fix("Python yfinance bridge", `"${py}" -m pip install yfinance`);
  }

  // 5b. Harness agent runtime (FastAPI + LangChain + LangGraph). The agentic
  //     harness delegates to this service, so a missing stack means runs are
  //     unavailable — install it, and report honestly if that fails.
  //     `langgraph` alone is NOT a sufficient marker: an interpreter with a
  //     partial install had langgraph but no provider packages, and every model
  //     call failed with "No module named 'langchain_openai'". Probe a provider
  //     too so the repair path actually fires.
  if (run(`"${py}" -c "import langgraph, langchain_openai"`, { timeout: 20000 }).status !== 0) {
    const requirements = path.join(root, "python", "requirements.txt");
    if (fs.existsSync(requirements)) {
      fix("Python agent runtime", `"${py}" -m pip install -r "${requirements}"`);
    }
  }

  // 5c. MCP client (Phase 5). `langgraph` presence does not imply `mcp` on an
  //     install predating it, so probe the MCP SDK explicitly.
  if (run(`"${py}" -c "import mcp"`, { timeout: 20000 }).status !== 0) {
    const requirements = path.join(root, "python", "requirements.txt");
    if (fs.existsSync(requirements)) {
      fix("Python MCP client", `"${py}" -m pip install -r "${requirements}"`);
    }
  }

  // 5d. JSON Schema validator. Skill input/output contracts are enforced with
  //     it, so a repair restores it explicitly rather than relying on it
  //     arriving transitively through `mcp`.
  if (run(`"${py}" -c "import jsonschema"`, { timeout: 20000 }).status !== 0) {
    const requirements = path.join(root, "python", "requirements.txt");
    if (fs.existsSync(requirements)) {
      fix("Python JSON Schema validator", `"${py}" -m pip install -r "${requirements}"`);
    }
  }

  // 5d-ter. The CHART ENGINE's dependencies. Charts are a core surface (the
  //        chat, the market workspace), so a missing stack is repaired here
  //        even when `langgraph` is already present: an older runtime install
  //        skipped `plotly`, leaving every chart as a static PNG at best. The
  //        full analysis requirements file covers numpy, pandas, matplotlib and
  //        plotly in one command.
  {
    // One cheap probe (module specs, not imports) so a healthy boot pays ~1 s
    // instead of importing four heavy packages.
    const probe = run(
      `"${py}" -c "import importlib.util; mods=['numpy','pandas','matplotlib','plotly']; print(','.join(m for m in mods if importlib.util.find_spec(m) is None))"`,
      { timeout: 30000 }
    );
    const missingCharts = (probe.stdout || "").trim().split(",").filter(Boolean);
    if (missingCharts.length > 0) {
      const analysisReq = path.join(root, "python", "analysis-requirements.txt");
      if (fs.existsSync(analysisReq)) {
        fix(
          `Python chart engine (missing: ${missingCharts.join(", ")})`,
          `"${py}" -m pip install -r "${analysisReq}"`
        );
        // VERIFY with the SAME interpreter, by actually importing (not just
        // find_spec). A pip install that exits 0 but leaves matplotlib
        // unimportable used to pass silently, and doctor still said "healthy"
        // while every chart was a 503.
        let verify = run(`"${py}" -c "import matplotlib, plotly, numpy"`, { timeout: 90000 });
        if (verify.status !== 0) {
          // The common real-world state is a dist-info that exists while the
          // package directory is gone, so `pip install` says "satisfied" and
          // changes nothing. Force a genuine reinstall once.
          console.log("[setup] Chart libraries installed but unimportable — force-reinstalling...");
          run(
            `"${py}" -m pip install --force-reinstall "matplotlib>=3.8" "plotly>=5.20" "numpy>=1.26" --disable-pip-version-check`,
            { stdio: ["ignore", "inherit", "inherit"], timeout: 600000 }
          );
          verify = run(`"${py}" -c "import matplotlib, plotly, numpy"`, { timeout: 90000 });
        }
        if (verify.status !== 0) {
          console.error(
            "[setup] CHART ENGINE UNAVAILABLE — the chart libraries cannot be imported."
          );
          console.error(`        Interpreter: ${py}`);
          console.error(`        Run: "${py}" -m pip install --force-reinstall -r "${analysisReq}"`);
          process.exit(1);
        }
      }
    }
  }

  // 5d-bis. Browser Automation Runtime — the Python Playwright package. OPTIONAL
  //         in effect: a missing one makes the browser toolset report an honest
  //         UNAVAILABLE (never a fake browser), so this is a best-effort repair.
  if (run(`"${py}" -c "import playwright"`, { timeout: 20000 }).status !== 0) {
    const requirements = path.join(root, "python", "requirements.txt");
    if (fs.existsSync(requirements)) {
      fix("Python browser runtime (optional)", `"${py}" -m pip install -r "${requirements}"`);
    }
  }

  // 5e. Browser binary presence. The Chromium build is a one-time download;
  //     doctor REPORTS it and names the exact command but never auto-downloads
  //     (keeping boot lightweight). A missing binary is an honest
  //     BROWSER_UNINSTALLED at runtime, never a fabricated browser.
  try {
    const probe = run(
      `"${py}" -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"`,
      { timeout: 25000 }
    );
    const exe = (probe.stdout || "").trim();
    const present = probe.status === 0 && exe && fs.existsSync(exe);
    console.log(
      present
        ? "[setup] browser binary: present"
        : `[setup] browser binary: not present — run "${py}" -m playwright install chromium once to enable browser tools`
    );
  } catch {
    console.log("[setup] browser binary: unknown (probe failed)");
  }

  // 5f. Real pseudo-terminal for the terminal subsystem's persistent shells
  //     (pywinpty on Windows, ptyprocess elsewhere). OPTIONAL: without it the
  //     session layer falls back to a piped shell and reports `pty: false`, so
  //     this is a best-effort repair, never a hard failure.
  const ptyModule = process.platform === "win32" ? "winpty" : "ptyprocess";
  if (run(`"${py}" -c "import ${ptyModule}"`, { timeout: 20000 }).status !== 0) {
    const requirements = path.join(root, "python", "requirements.txt");
    if (fs.existsSync(requirements)) {
      fix("Python PTY support (optional)", `"${py}" -m pip install -r "${requirements}"`);
    }
  }

  // 5f-bis. Unified search ANN index (sqlite-vec). OPTIONAL in effect: without it
  //         semantic search uses brute-force cosine and reports `annBackend:
  //         false` — still correct, never faked. Best-effort repair via the
  //         requirements file.
  if (run(`"${py}" -c "import sqlite_vec"`, { timeout: 20000 }).status !== 0) {
    const requirements = path.join(root, "python", "requirements.txt");
    if (fs.existsSync(requirements)) {
      fix("Python search ANN index (optional)", `"${py}" -m pip install -r "${requirements}"`);
    }
    console.log("[setup] search ANN index (sqlite-vec): absent - semantic search will use brute-force cosine");
  }

  // 5g. Computer Use Engine backends. OPTIONAL: each missing one degrades to an
  //     honest UNAVAILABLE with a reason (never a fake desktop), so these are
  //     best-effort repairs. Pillow powers capture; uiautomation is the rich
  //     accessibility tree; winsdk is the OS-native OCR engine.
  const cuaModules = [["PIL", "Pillow (computer-use capture)"], ["uiautomation", "UI Automation (computer-use accessibility)"]];
  if (process.platform === "win32") cuaModules.push(["winsdk", "Windows OCR (computer-use OCR)"]);
  for (const [mod, label] of cuaModules) {
    if (run(`"${py}" -c "import ${mod}"`, { timeout: 20000 }).status !== 0) {
      const requirements = path.join(root, "python", "requirements.txt");
      if (fs.existsSync(requirements)) {
        fix(label, `"${py}" -m pip install -r "${requirements}"`);
      }
    }
  }
} else {
  console.log("[setup] Python not found — live prices and the agent runtime are disabled.");
  console.log("        Install from https://python.org, then:");
  console.log("          pip install yfinance");
  console.log("          pip install -r python/requirements.txt");
}

// 6. Phase 11 — delivery / execution-backend runtime dependencies. These are
//    reported HONESTLY: a missing optional backend reads "not available" and the
//    run continues; only a MISSING vendored interpreter (which the agent runtime
//    genuinely requires) prints its exact repair command and exits non-zero.
{
  // 6a. Vendored Python (the interpreter every command and the agent runtime use).
  //     The Windows provisioning above normally creates it. If it is still absent,
  //     print the exact repair command and exit non-zero — never a silent partial.
  const vendored = path.join(__dirname, "vendor", "python", process.platform === "win32" ? "python.exe" : path.join("bin", "python3"));
  if (process.platform === "win32" && !fs.existsSync(vendored)) {
    console.error(
      "[setup] The bundled Python runtime is missing. Repair it with:\n" +
      "          npm run setup:python\n" +
      "        (or npm install). The agent runtime cannot run without it."
    );
    process.exit(1);
  } else {
    console.log(`[setup] vendored python: ${fs.existsSync(vendored) ? "present" : "not present (system python only)"}`);
  }

  // 6a-bis. The SANDBOX's numerical stack, in the interpreter the sandbox runs.
  //     Reported with the versions, so "the sandbox is not up to date" is
  //     visible without guessing: a missing stack is a repair command, never a
  //     silent partial.
  {
    const sandboxPy = fs.existsSync(vendored) ? vendored : null;
    if (!sandboxPy) {
      console.log("[setup] analysis stack: not reported (no vendored interpreter)");
    } else {
      const versions = ["numpy", "pandas", "matplotlib", "plotly"]
        .map((mod) => {
          try {
            const out = execSync(`"${sandboxPy}" -c "import ${mod} as m; print(m.__version__)"`, {
              encoding: "utf8",
              stdio: ["ignore", "pipe", "ignore"],
              timeout: 20000,
              windowsHide: true,
            });
            return `${mod}=${String(out).trim()}`;
          } catch {
            return `${mod}=MISSING`;
          }
        })
        .join(" ");
      if (versions.includes("MISSING")) {
        console.warn(
          `[setup] analysis stack: INCOMPLETE (${versions}) — a computed result that needs it will fail. Repair with:\n` +
          `          npm run setup:python`
        );
      } else {
        console.log(`[setup] analysis stack: ready (${versions})`);
      }
    }
  }

  // 6b. Artifact directory (saved job reports). Create it when writable; report
  //     honestly and name the path when it is not.
  const artifacts = path.join(root, ".black-eclipse", "artifacts");
  try {
    fs.mkdirSync(artifacts, { recursive: true });
    fs.accessSync(artifacts, fs.constants.W_OK);
    console.log(`[setup] artifact directory: writable (${path.relative(root, artifacts)})`);
  } catch (err) {
    console.warn(
      `[setup] artifact directory: NOT writable (${artifacts}) — saved job reports will be unavailable. ${err.message}`
    );
  }

  // 6c. Optional sandbox backends. DETECTED, never installed. Their absence is a
  //     refusal for a job that needs one — never a reason not to start.
  const containerRuntime = process.env.HARNESS_SANDBOX_CONTAINER || null;
  const containerUsable =
    containerRuntime && spawnSync(containerRuntime, ["--version"], { stdio: "ignore", windowsHide: true }).status === 0;
  console.log(
    containerUsable
      ? `[setup] container backend: available (${containerRuntime}) for jobs; agent commands still run as host processes`
      : "[setup] sandbox: no container backend — agent commands run as host processes (process-tree limits only). " +
        "Set BLACK_ECLIPSE_SANDBOX_REQUIRED=1 to refuse them instead; install Docker for a real boundary."
  );

  const remoteUrl = process.env.HARNESS_SANDBOX_REMOTE_URL || null;
  console.log(
    remoteUrl
      ? "[setup] remote backend: configured"
      : "[setup] remote backend: not configured"
  );

  // 6c-bis. Native video worker (OPTIONAL). REPORTED, never built here: a cargo
  //     build needs FFMPEG_DIR (the FFmpeg dev tree) + LIBCLANG_PATH for
  //     bindgen, and minutes of CPU — doctor must stay lightweight. Absence is
  //     an honest "not ready" in the video capabilities, never a fake render.
  const videoEnabled = !["0", "false", "no", "off"].includes(
    (process.env.HARNESS_VIDEO_ENABLED || "1").trim().toLowerCase()
  );
  if (!videoEnabled) {
    console.log("[setup] video worker: disabled (HARNESS_VIDEO_ENABLED=0)");
  } else {
    const videoBin =
      (process.env.HARNESS_VIDEO_WORKER_BIN || "").trim() ||
      path.join(
        root, "rust", "fframes-worker", "target", "release",
        process.platform === "win32" ? "fframes-worker.exe" : "fframes-worker"
      );
    const ffmpegDir = (
      process.env.HARNESS_VIDEO_FFMPEG_DIR || process.env.FFMPEG_DIR || ""
    ).trim();
    console.log(
      fs.existsSync(videoBin)
        ? `[setup] video worker: present (${path.relative(root, videoBin)})`
        : "[setup] video worker: not built — run \"cargo build --release\" in rust/fframes-worker (video tools report NOT ready until then)"
    );
    if (!ffmpegDir) {
      console.log(
        "[setup] video ffmpeg: FFMPEG_DIR not set — the worker needs the FFmpeg DLLs on PATH (see docs/video-pipeline.md)"
      );
    } else if (!fs.existsSync(ffmpegDir)) {
      console.log(`[setup] video ffmpeg: FFMPEG_DIR points at a missing directory: ${ffmpegDir}`);
    } else {
      console.log("[setup] video ffmpeg: configured");
    }
  }

  // 6d. Authentication readiness. Authentication is OPT-IN and auto-engages the
  //     moment the app is reachable beyond loopback (or is a production build).
  //     This reports which mode will apply and whether it is actually usable, so a
  //     public deployment is never silently left open NOR silently locked out.
  const authRaw = (process.env.HARNESS_AUTH || "").trim().toLowerCase();
  const authExplicit = ["1", "true", "yes", "on", "required", "enable", "enabled"].includes(authRaw)
    ? true
    : ["0", "false", "no", "off", "none", "disabled"].includes(authRaw)
      ? false
      : undefined;
  const bindHost = (process.env.HOST || "").trim().toLowerCase();
  const loopback = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
  const publicBind = Boolean(bindHost) && !loopback.has(bindHost);
  const production = process.env.NODE_ENV === "production";
  const authRequired = authExplicit === undefined ? publicBind || production : authExplicit;
  const authSecret = process.env.HARNESS_AUTH_SECRET;
  const secretOk = Boolean(authSecret) && authSecret !== "change-me-to-a-random-secret" && authSecret.length >= 16;
  if (!authRequired) {
    console.log(
      "[setup] auth: off (single-user local; run `npm run auth:setup` before exposing this beyond loopback)",
    );
  } else if (secretOk) {
    console.log("[setup] auth: required and configured");
  } else {
    console.warn(
      "[setup] auth: REQUIRED but HARNESS_AUTH_SECRET is not set; requests will fail closed (503).\n" +
      "        Run `npm run auth:setup` to generate one and set HARNESS_AUTH=required,\n" +
      "        or set HARNESS_AUTH=off to run accountless on a trusted local-only machine."
    );
  }
}

// 9. Privacy posture — make the two data-boundary settings visible at boot.
{
  const privacy = (process.env.ECLIPSE_PRIVACY || readDotEnvValue("ECLIPSE_PRIVACY") || "").trim();
  const localModels = /local/i.test(privacy);
  console.log(
    localModels
      ? "[setup] privacy: local-models (remote model endpoints are refused)"
      : "[setup] privacy: any configured provider (set ECLIPSE_PRIVACY=local-models to block cloud models)",
  );
  const vault = process.env.BE_VAULT_PATH || readDotEnvValue("BE_VAULT_PATH");
  console.log(
    vault
      ? `[setup] data: encrypted vault at ${vault}`
      : "[setup] data: repo folder (not encrypted at rest - see docs/data-and-privacy.md)",
  );
}

if (fixed > 0) console.log(`[setup] done — ${fixed} item(s) fixed.`);