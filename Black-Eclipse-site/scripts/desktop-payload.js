#!/usr/bin/env node
// Build a self-contained Next.js payload for the desktop app.
//
// Why a separate script: a normal build must never be disturbed. This one writes
// to `.next-desktop` (its own distDir) with `output: "standalone"`, so the live
// production server keeps serving `.next` untouched.
//
// What "standalone" buys us: Next traces the files the server needs into one
// folder instead of requiring the whole 1.9 GB `node_modules`. What it does NOT
// do is respect the repository's hygiene — it mirrors local configuration
// (`.env`, `mcp-servers.json`) and whole source trees. So the payload is pruned
// and then asserted clean before anyone can ship it.
//
// This step does not package Node, Python or the installer. It produces the app
// payload and reports its size.

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const { buildEnv } = require("./lib/server");

const ROOT = path.join(__dirname, "..");
const DIST = ".next-desktop";
const STANDALONE = path.join(ROOT, DIST, "standalone");
const TSCONFIG = path.join(ROOT, "tsconfig.json");

function dirSizeMB(dir) {
  let total = 0;
  const walk = (d) => {
    for (const entry of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (entry.isFile()) {
        try {
          total += fs.statSync(p).size;
        } catch {}
      }
    }
  };
  try {
    walk(dir);
  } catch {}
  return total / (1024 * 1024);
}

function copyDir(from, to) {
  if (!fs.existsSync(from)) {
    console.log(`  skip (missing): ${path.relative(ROOT, from)}`);
    return;
  }
  fs.mkdirSync(to, { recursive: true });
  fs.cpSync(from, to, { recursive: true });
  console.log(`  copied ${path.relative(ROOT, from)} -> ${path.relative(ROOT, to)}`);
}

/* -------------------------------------------------------------- hygiene */

// Anything here must never appear in a payload handed to a user.
const LEAK_PATHS = [
  ".env",
  ".env.local",
  ".env.production",
  ".env.development",
  "mcp-servers.json",
  ".connector-key",
  "jdds.db",
  "jdds.db-wal",
  "jdds.db-shm",
  "agent-memory.db",
  "agent-memory.db-wal",
  "agent-memory.db-shm",
  "eclipse-memory.db",
  "eclipse-memory.db-wal",
  "eclipse-memory.db-shm",
  "python/.black-eclipse/channels/secret.key",
];

// Trees the compiled server never reads at run time.
const PRUNE_PATHS = [
  ...LEAK_PATHS,
  ".black-eclipse",
  "python/.black-eclipse",
  ".next-dev",
  // Local build/release/runtime trees. Next's standalone tracing copies the
  // whole checkout root, and these grew into gigabytes (a promoted release
  // cache, the desktop runtime cache and other dist dirs). They are local
  // state: shipping them bloated the payload past what the 32-bit NSIS
  // compressor can map, and none of them is read by `server.js`.
  // NOTE: `.next-desktop` is deliberately NOT listed: it is the real build in
  // an assembled payload (`--prune-only`), and the build path already removes
  // the stray nested copy before renaming `.next` to it.
  ".next-desktop-dev",
  ".next-live-stable",
  ".runtime",
  "releases",
  "release",
  "dist",
  ".tmp-tests",
  "backups",
  "deploy",
  "desktop",
  "docs",
  "node_modules/.cache",
  "pine-engine",
  "rust",
  "sdk",
  "src",
  "tsconfig.tsbuildinfo",
];

function pruneStandalone() {
  let removed = 0;
  for (const rel of PRUNE_PATHS) {
    const target = path.join(STANDALONE, rel);
    if (fs.existsSync(target)) {
      fs.rmSync(target, { recursive: true, force: true });
      removed += 1;
    }
  }
  for (const entry of fs.readdirSync(STANDALONE)) {
    if (/\.log$/i.test(entry) || /\.(db|db-wal|db-shm)$/i.test(entry)) {
      fs.rmSync(path.join(STANDALONE, entry), { force: true });
      removed += 1;
    }
  }
  console.log(`  pruned ${removed} path(s) that must not ship`);

  const leaks = LEAK_PATHS.filter((rel) => fs.existsSync(path.join(STANDALONE, rel)));
  const strayEnv = fs.readdirSync(STANDALONE).filter((e) => /^\.env/i.test(e));
  const strayDb = fs.readdirSync(STANDALONE).filter((e) => /\.(db|db-wal|db-shm)$/i.test(e));
  if (leaks.length || strayEnv.length || strayDb.length) {
    console.error(
      `\n[payload] REFUSING to hand over a payload containing local configuration: ` +
        `${[...leaks, ...strayEnv, ...strayDb].join(", ")}`
    );
    process.exit(3);
  }
}

/* ---------------------------------------------------------------- main */

if (process.argv.includes("--prune-only")) {
  if (!fs.existsSync(path.join(STANDALONE, "server.js"))) {
    console.error("Nothing to prune: no standalone output.");
    process.exit(2);
  }
  console.log("Pruning existing payload\n");
  pruneStandalone();
  console.log(`\n  standalone payload : ${dirSizeMB(STANDALONE).toFixed(0)} MB`);
  process.exit(0);
}

console.log("Building standalone payload into " + DIST + "\n");

const env = buildEnv({
  NODE_ENV: "production",
  NEXT_DIST_DIR: DIST,
  BE_STANDALONE: "1",
  NEXT_TELEMETRY_DISABLED: "1",
});

// Next rewrites `tsconfig.json` `include` to add the new distDir. That is a
// tracked file shared with everyone, so snapshot it and put it back afterwards.
const tsconfigBefore = fs.existsSync(TSCONFIG) ? fs.readFileSync(TSCONFIG) : null;

const result = spawnSync(
  process.execPath,
  [path.join(ROOT, "node_modules", "next", "dist", "bin", "next"), "build"],
  { cwd: ROOT, env, stdio: "inherit", windowsHide: true },
);

if (tsconfigBefore) {
  try {
    if (!fs.readFileSync(TSCONFIG).equals(tsconfigBefore)) {
      fs.writeFileSync(TSCONFIG, tsconfigBefore);
      console.log("\nRestored tsconfig.json (Next had added the payload distDir to `include`).");
    }
  } catch {}
}

if (result.status !== 0) {
  console.error(`\nBuild failed with exit ${result.status}. Nothing was moved.`);
  process.exit(result.status || 1);
}

if (!fs.existsSync(path.join(STANDALONE, "server.js"))) {
  console.error(
    `\nExpected ${path.relative(ROOT, STANDALONE)}/server.js — the standalone output layout changed. ` +
      `Payload not assembled.`
  );
  process.exit(2);
}

// Next's standalone output deliberately omits these; its docs require copying
// them next to server.js.
console.log("\nAssembling:");
copyDir(path.join(ROOT, DIST, "static"), path.join(STANDALONE, ".next", "static"));
copyDir(path.join(ROOT, "public"), path.join(STANDALONE, "public"));
pruneStandalone();

// The standalone `server.js` bakes in the build's distDir ("./.next-desktop"),
// while Next writes its output to ".next" inside the payload. Rename it — and
// delete the stray recursive copy of the build dir that Next leaves behind
// (only when `.next` still exists, so a re-run is idempotent).
const payloadNext = path.join(STANDALONE, ".next");
const bakedDist = path.join(STANDALONE, DIST);
if (fs.existsSync(payloadNext)) {
  if (fs.existsSync(bakedDist)) {
    fs.rmSync(bakedDist, { recursive: true, force: true });
    console.log(`  removed stray ${DIST} copy nested in the payload`);
  }
  fs.renameSync(payloadNext, bakedDist);
  console.log(`  renamed .next -> ${DIST} (matches the baked-in distDir)`);
}

// Next's standalone output omits BUILD_ID (it is consumed at build time), yet
// `next start` treats a missing BUILD_ID as "no production build". Carry it over.
const buildIdSrc = path.join(ROOT, DIST, "BUILD_ID");
const buildIdDst = path.join(STANDALONE, DIST, "BUILD_ID");
if (fs.existsSync(buildIdSrc) && !fs.existsSync(buildIdDst)) {
  fs.copyFileSync(buildIdSrc, buildIdDst);
  console.log("  copied BUILD_ID into the payload");
}

// The traced server tree is incomplete — `server/app/_not-found/page.js` is
// absent, and the packaged server dies on the first request with MODULE_NOT_FOUND.
// Overlay the authoritative build over the traced subset.
copyDir(path.join(ROOT, DIST, "server"), path.join(STANDALONE, DIST, "server"));

// The standalone output carries only a subset of the top-level build manifests.
// On the macOS runner that produced "ENOENT ... routes-manifest.json" at startup:
// Next expects the full set beside its build. Copy every top-level FILE (never
// the directories, which are already handled or are the multi-GB cache).
const buildRoot = path.join(ROOT, DIST);
const payloadRoot = path.join(STANDALONE, DIST);
let manifestCount = 0;
for (const entry of fs.readdirSync(buildRoot, { withFileTypes: true })) {
  if (!entry.isFile()) continue;
  fs.copyFileSync(path.join(buildRoot, entry.name), path.join(payloadRoot, entry.name));
  manifestCount += 1;
}
console.log(`  copied ${manifestCount} top-level build file(s)`);

console.log("\nSizes:");
console.log(`  standalone payload : ${dirSizeMB(STANDALONE).toFixed(0)} MB`);
console.log(`\nRun it with: node ${path.relative(ROOT, path.join(STANDALONE, "server.js"))}`);
console.log("The website server (3105, serving .next) and the desktop pin (3100) were not touched.");
