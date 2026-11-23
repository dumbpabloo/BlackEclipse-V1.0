#!/usr/bin/env node
// `npm run desktop:refresh` — build (if needed), promote, reopen Stable.
//
// This is the desktop-only replacement for the old "restart the 3105 website"
// dance. It never touches a browser server, never kills a build it did not
// start, and stages the new build before the window is restarted.
//
// Flags: --no-build (promote the existing build) · --channel dev

const fs = require("fs");
const path = require("path");
const { spawnSync } = require("child_process");

const { channelFor } = require("./lib/channels");
const { needsBuild, buildInProgress } = require("./lib/server");
const { buildLooksComplete, readBuildId } = require("./lib/desktop-release");
const {
  installedShellPath,
  stopShell,
  stopOwnedServer,
} = require("./lib/desktop-process");

const ROOT = path.resolve(__dirname, "..");
const argv = process.argv.slice(2);
const noBuild = argv.includes("--no-build");
const noRestart = argv.includes("--no-restart");
const channel = channelFor(argv.indexOf("--channel") >= 0 ? argv[argv.indexOf("--channel") + 1] : "stable");
// Promote an explicit build (e.g. a verified `.next-desktop-dev`) without a
// second identical compile. The source is still validated by the promoter.
const from = argv.indexOf("--from") >= 0 ? argv[argv.indexOf("--from") + 1] : null;
// The version of the build being promoted, when the caller knows it (the
// updater reads it from the signed manifest). Forwarded so the release metadata
// records the release that was installed, not the checkout on disk.
const version = argv.indexOf("--version") >= 0 ? argv[argv.indexOf("--version") + 1] : null;

function log(message) {
  console.log(`[desktop:refresh] ${message}`);
}

function run(script, args) {
  const result = spawnSync(process.execPath, [path.join(__dirname, script), ...args], {
    cwd: ROOT,
    stdio: "inherit",
    windowsHide: true,
  });
  return result.status === 0;
}

function isBridged() {
  try {
    return fs
      .readFileSync(path.join(path.dirname(installedShellPath()), "app", "scripts", "desktop-server.js"), "utf8")
      .includes("desktop-dev-bridge.js");
  } catch {
    return false;
  }
}

/**
 * Point the installed Stable shell at this checkout before promoting.
 *
 * A packaged install serves the frozen payload under `<install>\app`, so a
 * refresh that only promotes `releases/stable/<id>/.next` changes nothing the
 * user sees: the window keeps loading the bundled UI. Bridging is reversible
 * (the bundled payload is renamed `app.bundled-<version>`, never deleted) and
 * is exactly what `npm run desktop:dev-bridge` does. Doing it here removes the
 * silent-stale-app trap. Stop the window/server first so Windows releases the
 * file handles inside `app` before the rename.
 */
function ensureBridged() {
  if (channel.id !== "stable") return true;
  if (!fs.existsSync(installedShellPath())) return true;
  if (isBridged()) return true;
  log("the installed app serves its bundled payload — bridging it to this checkout…");
  stopShell(channel);
  stopOwnedServer(channel);
  if (!run("desktop-dev-bridge.js", []) || !isBridged()) {
    console.error(
      '[desktop:refresh] could not bridge the installed app to this checkout — ' +
        'run "npm run desktop:dev-bridge" and read its output',
    );
    return false;
  }
  log("installed app now serves this checkout");
  return true;
}

function main() {
  const running = buildInProgress();
  if (running) {
    console.error(
      `[desktop:refresh] a production build is already running (pid ${running.pid}). ` +
        "Wait for it to finish, then run this again. Refresh never races a build.",
    );
    process.exit(1);
  }

  const source = from ? path.resolve(ROOT, from) : path.join(ROOT, channel.buildDir);
  const buildNeeded =
    !noBuild && !from && (channel.id === "dev" ? true : needsBuild());
  if (buildNeeded) {
    log("checking/building the payload (slow step)…");
    const args = ["--channel", channel.id];
    if (channel.id === "dev") args.push("--if-needed");
    const built = spawnSync(process.execPath, [path.join(__dirname, "desktop-build.js"), ...args], {
      cwd: ROOT,
      stdio: "inherit",
      windowsHide: true,
    });
    if (built.status !== 0) {
      console.error("[desktop:refresh] build failed — the running app was not touched");
      process.exit(1);
    }
  } else {
    log(noBuild ? "using the existing build (--no-build)" : "build is current");
  }

  if (!buildLooksComplete(source)) {
    console.error(`[desktop:refresh] no completed build in ${from || channel.buildDir}`);
    process.exit(1);
  }
  // Bridge AFTER the build so the old window keeps working during the slow
  // compile; the promote step restarts it anyway.
  if (!ensureBridged()) {
    console.error("[desktop:refresh] the build is ready, but the window was left untouched");
    process.exit(1);
  }
  log(`promoting ${channel.id} build ${readBuildId(source)}…`);
  const promoteArgs = ["--channel", channel.id];
  if (from) promoteArgs.push("--from", from);
  if (version) promoteArgs.push("--version", version);
  if (noRestart) promoteArgs.push("--no-restart");
  if (!run("desktop-promote.js", promoteArgs)) {
    console.error("[desktop:refresh] promotion failed — the running app was not touched");
    process.exit(1);
  }
  log("done");
}

main();
