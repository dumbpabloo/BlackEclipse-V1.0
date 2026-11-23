#!/usr/bin/env node
// `npm run desktop:promote` — atomically make a completed build the channel's
// current release, with a rollback target.
//
//   node scripts/desktop-promote.js                     # promote Stable (.next)
//   node scripts/desktop-promote.js --channel dev       # promote Dev payload
//   node scripts/desktop-promote.js --rollback          # flip to the previous release
//
// Safety contract:
//   * never promotes while another build is running;
//   * copies into a temporary directory and renames, so a partial copy can
//     never become current;
//   * validates the copied build (BUILD_ID, routes manifest, server + static,
//     per-build static manifest) BEFORE moving the pointer;
//   * keeps the previous release and never deletes user data;
//   * restarts only the target channel's own window/server.

const fs = require("fs");
const path = require("path");

const { channelFor } = require("./lib/channels");
const { buildInProgress } = require("./lib/server");
const {
  ROOT,
  readBuildId,
  buildLooksComplete,
  copyRuntime,
  readPointer,
  writePointer,
} = require("./lib/desktop-release");
const {
  shellPathFor,
  stopShell,
  stopOwnedServer,
  launchShell,
  waitForShellExit,
  shellPids,
  pidAlive,
} = require("./lib/desktop-process");

function argValue(flag) {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

const rollback = process.argv.includes("--rollback");
const noRestart = process.argv.includes("--no-restart");
const channel = channelFor(argValue("--channel") || "stable");
// An explicit source build. `--from .next-desktop-dev` lets a verified Dev
// build serve Stable without a second identical compile. Omitted: the channel's
// own build dir.
const fromArg = argValue("--from");
// The version of the build being promoted. A signed release artifact carries no
// `package.json`, so the promoter cannot read it from the build — the updater
// passes the version from the signed manifest. Without this the release metadata
// claimed the CHECKOUT's version, which is how a window ended up reporting
// "up to date" for a version it was not running. Omitted: the checkout's own
// version (a local build, where that is the truth).
const versionArg = (() => {
  const value = argValue("--version");
  if (!value) return null;
  if (!/^v?\d+\.\d+\.\d+/.test(value.trim())) {
    console.error(`[desktop:promote] ignoring invalid --version "${value}"`);
    return null;
  }
  return value.trim().replace(/^v/, "");
})();

function log(message) {
  console.log(`[desktop:promote:${channel.id}] ${message}`);
}

function fail(message) {
  console.error(`[desktop:promote:${channel.id}] ${message}`);
  process.exit(1);
}

function relative(from) {
  return path.relative(ROOT, from).split(path.sep).join("/");
}

function pointerInfo(dist, version) {
  return {
    dir: relative(dist),
    buildId: readBuildId(dist),
    version: version || readVersion(),
    channel: channel.id,
    promotedAt: new Date().toISOString(),
  };
}

async function waitPortFree(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      await fetch(`http://127.0.0.1:${port}/`, {
        redirect: "manual",
        signal: AbortSignal.timeout(1200),
      });
    } catch {
      return true;
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  return false;
}

async function restart() {
  const exe = shellPathFor(channel);
  if (!fs.existsSync(exe)) {
    log(`window binary not found: ${exe}`);
    log("the release pointer is updated; launch the channel when the binary is available");
    return;
  }
  log("stopping this channel's window and server…");
  const pids = stopShell(channel);
  if (pids.length) log(`stopped window pid ${pids.join(", ")}`);
  // WAIT for the window to actually exit before doing anything else. Tauri's
  // single-instance plugin hands a new launch to the still-exiting old process,
  // so relaunching too early starts nothing: the app sits on a black splash with
  // no server. That was the "update leaves a dead window" failure.
  if (pids.length && !waitForShellExit(channel)) {
    log("the previous window is taking a while to exit — relaunching anyway");
  }
  stopOwnedServer(channel);
  const free = await waitPortFree(channel.appPort, 15_000);
  if (!free) log(`port ${channel.appPort} is still busy — the next launch will report it`);
  let launched = launchShell(channel);
  if (!launched.launched) {
    log(`could not relaunch the window (${launched.reason || "unknown"}) — open it manually`);
    return;
  }
  // A spawn handed off to a still-exiting instance exits immediately and leaves
  // NO window at all. Confirm the process survives, and retry once after the old
  // one is gone, so "Restart" can never end with an empty desktop.
  await new Promise((r) => setTimeout(r, 2_500));
  if (launched.pid && !pidAlive(launched.pid) && shellPids(exe).length === 0) {
    log("the relaunched window exited immediately — retrying");
    waitForShellExit(channel);
    launched = launchShell(channel);
  }
  if (launched.launched) {
    log(`window relaunched (pid ${launched.pid})`);
  } else {
    log("the window could not be relaunched — open Black Eclipse again");
  }
}

function rollbackRelease() {
  const current = readPointer(channel, "current");
  const previous = readPointer(channel, "previous");
  if (!previous) fail("no previous release to roll back to");
  if (!previous.valid) fail(`previous release is missing/incomplete: ${previous.dir}`);

  log(`rolling back ${current && current.buildId ? current.buildId : "current"} → ${previous.buildId}`);
  if (current && current.valid) {
    writePointer(channel, "previous", { ...current, demotedAt: new Date().toISOString() });
  }
  writePointer(channel, "current", {
    dir: previous.dir,
    buildId: previous.buildId,
    channel: channel.id,
    promotedAt: previous.promotedAt || new Date().toISOString(),
    rolledBackFrom: current ? current.buildId : null,
  });
  log(`current is now ${previous.buildId}`);
}

function promoteRelease() {
  const running = buildInProgress();
  if (running) {
    fail(
      `a production build is running (pid ${running.pid}). Finish it, then promote. ` +
        "Promotion never races a build.",
    );
  }

  const source = fromArg ? path.resolve(ROOT, fromArg) : path.join(ROOT, channel.buildDir);
  if (!buildLooksComplete(source)) {
    fail(
      `no completed build in ${fromArg || channel.buildDir}. Run ` +
        `${channel.id === "dev" ? '"npm run desktop:dev"' : '"npm run desktop:refresh"'} first.`,
    );
  }
  const buildId = readBuildId(source);
  const releaseDir = path.join(channel.releasesDir, buildId);
  const distDir = path.join(releaseDir, ".next");

  if (!buildLooksComplete(distDir)) {
    const temp = `${releaseDir}.tmp`;
    log(`staging ${buildId} into ${relative(releaseDir)}…`);
    fs.rmSync(temp, { recursive: true, force: true });
    try {
      copyRuntime(source, path.join(temp, ".next"));
      if (!buildLooksComplete(path.join(temp, ".next"))) {
        throw new Error("copied build failed validation");
      }
      fs.writeFileSync(
        path.join(temp, "manifest.json"),
        JSON.stringify(
          {
            channel: channel.id,
            buildId,
            version: versionArg || readVersion(),
            createdAt: new Date().toISOString(),
            source: relative(source),
          },
          null,
          2,
        ),
      );
      fs.mkdirSync(channel.releasesDir, { recursive: true });
      fs.rmSync(releaseDir, { recursive: true, force: true });
      fs.renameSync(temp, releaseDir);
    } catch (err) {
      fs.rmSync(temp, { recursive: true, force: true });
      fail(`staging failed: ${err && err.message ? err.message : err}`);
    }
  } else {
    log(`release ${buildId} already staged`);
  }

  // Validation happens BEFORE the pointer moves.
  if (!buildLooksComplete(distDir)) fail("staged release is not serviceable; current left untouched");

  const current = readPointer(channel, "current");
  if (current && current.valid) {
    writePointer(channel, "previous", {
      dir: current.dir,
      buildId: current.buildId,
      version: current.version || readVersion(),
      channel: channel.id,
      promotedAt: current.promotedAt || null,
    });
    log(`previous release kept: ${current.buildId}`);
  }

  // An already-staged release directory carries the version it was FIRST
  // promoted with — which, before `--version` existed, was the checkout's
  // version. A signed manifest is authority, so it repairs the metadata too:
  // the release the window reports must be the release it runs.
  const manifestFile = path.join(releaseDir, "manifest.json");
  const staged = readJson(manifestFile);
  const effectiveVersion =
    versionArg || (staged && typeof staged.version === "string" ? staged.version : readVersion());
  if (versionArg && staged && staged.version !== versionArg) {
    try {
      fs.writeFileSync(manifestFile, JSON.stringify({ ...staged, version: versionArg }, null, 2));
      log(`release metadata version corrected to ${versionArg}`);
    } catch {
      /* read-only release dir: the pointer below still carries the truth */
    }
  }
  writePointer(channel, "current", pointerInfo(distDir, effectiveVersion));
  log(`current is now ${buildId}`);
}

/** Keep only the current + previous release directories (each ~260 MB). */
function pruneReleases() {
  const current = readPointer(channel, "current");
  const previous = readPointer(channel, "previous");
  const keep = new Set();
  for (const p of [current, previous]) {
    if (!p || !p.dir) continue;
    // dir is `releases/<channel>/<buildId>/.next`
    const dir = path.resolve(ROOT, p.dir);
    keep.add(path.basename(path.dirname(dir)));
  }
  let removed = 0;
  try {
    for (const entry of fs.readdirSync(channel.releasesDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      if (keep.has(entry.name)) continue;
      fs.rmSync(path.join(channel.releasesDir, entry.name), { recursive: true, force: true });
      removed += 1;
    }
  } catch {}
  if (removed) log(`pruned ${removed} old release folder(s)`);
}

function readVersion() {
  try {
    return JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8")).version || null;
  } catch {
    return null;
  }
}

function readJson(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

async function main() {
  if (rollback) rollbackRelease();
  else promoteRelease();
  pruneReleases();
  if (noRestart) {
    // Used by the in-app updater: the pointer moves now, the running window
    // keeps serving the build it already loaded, and the shell (or the user)
    // restarts when it is ready. Safe because the pointer write is atomic and
    // the running release is kept as `previous`.
    log("release promoted without restart (--no-restart)");
  } else {
    await restart();
  }
  log("done");
}

main().catch((err) => fail(err && err.message ? err.message : String(err)));
