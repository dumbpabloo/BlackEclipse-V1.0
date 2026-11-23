// Dependency installs that cannot damage a running install.
//
// The old path was `npm ci` straight into `<app>/node_modules`. `npm ci` deletes
// the tree before it installs, so on Windows — where a loaded `.node` binary
// cannot be deleted — the delete fails halfway and the user is left with a
// gutted `node_modules`: react missing, `next/` empty, `.prisma` gone. The app
// keeps answering from modules it already has in memory and then dies on the
// next launch. That is exactly what happened on 2026-11-22 (v0.4.24 → v0.4.25).
//
// Two facts make the damage avoidable, both verified on Windows:
//
//   * A loaded `.node` file CAN be renamed, but not deleted. So a rename probe
//     is not a lock test (the old `update-locks.js` believed it was), and no
//     cheap "is it locked?" check exists. Probing was the wrong tool.
//   * A DIRECTORY containing loaded files CAN be renamed while the process that
//     mapped them keeps running. So a whole `node_modules` tree can be swapped
//     by rename, atomically, with no shutdown and no deletes on the critical
//     path.
//
// The install therefore happens in a staging directory and is swapped in only
// after npm has succeeded. A failed install (file lock, antivirus, network,
// disk) leaves the live tree byte-for-byte untouched, and the running app is
// unaffected until its normal restart.
//
// The root project's own lifecycle scripts (`postinstall` = prisma generate,
// python stack, hooks) are project-relative and must run against the real tree,
// so they are stripped from the staged manifest and run in order after the swap.

"use strict";

const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const STAGE_REL = path.join(".runtime", "deps-stage");
const PREVIOUS_REL = path.join(".runtime", "deps-previous");

// Packages whose absence means npm "succeeded" on a tree that cannot boot.
const CANARIES = ["next", "react", "react-dom"];

// Run after the swap, in this order. `prepare` is included because npm runs it
// on install for local trees; leaving it out of the stage keeps the stage a pure
// dependency fetch.
const DEFERRED_SCRIPTS = ["preinstall", "install", "postinstall", "prepare"];

// npm behaviour depends on the environment it inherits. The desktop app runs
// with `NODE_ENV=production`, and a staged install under that env silently
// installs production packages only — which is right for serving the compiled
// build but wrong for a source checkout, where `npm run build`, the type checker
// and the test suite need the dev tree. The updater therefore installs the same
// tree `npm ci` produces in a normal shell, whatever env started it.
function installEnv(env) {
  const next = { ...env };
  delete next.NODE_ENV;
  delete next.npm_config_production;
  delete next.npm_config_omit;
  return next;
}

function defaultRunInstall(cwd, env) {
  execSync("npm ci --no-audit --no-fund --include=dev", {
    cwd,
    stdio: "inherit",
    env: installEnv(env),
  });
}

function defaultRunScript(cwd, name, env) {
  execSync(`npm run ${name}`, { cwd, stdio: "inherit", env: installEnv(env) });
}

/**
 * The staged `package.json`: identical dependencies, minus the scripts that
 * only make sense in the real tree. `npm ci` validates dependencies against the
 * lockfile, which is unaffected by removing scripts.
 */
function stagedManifest(root) {
  const raw = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  const pkg = { ...raw };
  if (pkg.scripts) {
    const scripts = { ...pkg.scripts };
    for (const name of DEFERRED_SCRIPTS) delete scripts[name];
    if (Object.keys(scripts).length) pkg.scripts = scripts;
    else delete pkg.scripts;
  }
  return pkg;
}

/** Scripts from the real manifest that must run after the swap. */
function deferredScripts(root) {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
    return DEFERRED_SCRIPTS.filter((name) => pkg.scripts && pkg.scripts[name]);
  } catch {
    return [];
  }
}

function copyIfPresent(from, to) {
  if (!fs.existsSync(from)) return false;
  fs.copyFileSync(from, to);
  return true;
}

/** A tree npm claims to have installed, or a throw with the reason it is not. */
function assertUsableTree(modules) {
  if (!fs.existsSync(modules)) throw new Error("npm reported success but produced no node_modules");
  if (!fs.existsSync(path.join(modules, ".package-lock.json"))) {
    throw new Error("the staged node_modules has no lockfile — the install did not complete");
  }
  const missing = CANARIES.filter((name) => !fs.existsSync(path.join(modules, name, "package.json")));
  if (missing.length) {
    throw new Error(`the staged node_modules is missing ${missing.join(", ")}`);
  }
}

/**
 * Fetch dependencies into `.runtime/deps-stage` and return that node_modules.
 * Nothing outside the stage is written, so any failure is a no-op for the app.
 */
function stageDependencies(root, { log = () => {}, env = process.env, runInstall } = {}) {
  const stage = path.join(root, STAGE_REL);
  const install = runInstall || defaultRunInstall;

  fs.rmSync(stage, { recursive: true, force: true });
  fs.mkdirSync(stage, { recursive: true });

  fs.writeFileSync(
    path.join(stage, "package.json"),
    JSON.stringify(stagedManifest(root), null, 2),
  );
  if (!copyIfPresent(path.join(root, "package-lock.json"), path.join(stage, "package-lock.json"))) {
    throw new Error("package-lock.json is missing — `npm ci` cannot run without it");
  }
  // Registry/auth settings from the project. npm also walks up to
  // `<root>/.npmrc`; copying makes the stage independent of that walk.
  copyIfPresent(path.join(root, ".npmrc"), path.join(stage, ".npmrc"));

  log("fetching dependencies into a staging directory (live install untouched)…");
  install(stage, env);

  const modules = path.join(stage, "node_modules");
  assertUsableTree(modules);
  return modules;
}

/** Move a leftover previous tree out of the way without deleting it. */
function parkPrevious(previous, log) {
  if (!fs.existsSync(previous)) return;
  try {
    fs.rmSync(previous, { recursive: true, force: true });
    return;
  } catch {
    // Still mapped by the running app: a delete cannot succeed until it exits.
    // Renaming is always allowed, so keep it under a unique name for cleanup.
    const parked = `${previous}-${Date.now()}`;
    try {
      fs.renameSync(previous, parked);
      log(`kept the previous dependency tree for cleanup: ${path.relative(previous, parked) || parked}`);
    } catch {
      /* the next run retries */
    }
  }
}

/**
 * Swap the staged tree into place. Two renames, no deletes: the live tree moves
 * aside and the verified tree moves in. If the second rename fails the first is
 * undone, so the app is never left without a complete tree.
 */
function swapIntoPlace(root, stagedModules, { log = () => {} } = {}) {
  const live = path.join(root, "node_modules");
  const previous = path.join(root, PREVIOUS_REL);
  parkPrevious(previous, log);

  const hadLive = fs.existsSync(live);
  if (hadLive) fs.renameSync(live, previous);
  try {
    fs.renameSync(stagedModules, live);
  } catch (err) {
    if (hadLive) {
      try {
        fs.renameSync(previous, live);
      } catch {
        /* reported below; the tree is still on disk as deps-previous */
      }
    }
    throw new Error(
      `the verified dependency tree could not be moved into place: ${err && err.message ? err.message : err}. ` +
        (hadLive
          ? "Your previous tree is intact — run `npm run doctor`."
          : "No dependency tree was installed."),
    );
  }
  return { previous: hadLive ? previous : null };
}

/** Best-effort removal of the tree the new install replaced. */
function dropPrevious(root, { log = () => {} } = {}) {
  const previous = path.join(root, PREVIOUS_REL);
  if (!fs.existsSync(previous)) return true;
  try {
    fs.rmSync(previous, { recursive: true, force: true });
    return true;
  } catch {
    // A running app still maps files from it. Harmless: parked trees are
    // removed by the next install, and never used for anything else.
    log("the replaced dependency tree is still in use and will be cleaned up on the next update");
    return false;
  }
}

/**
 * Remove trees parked by earlier runs (a running app keeps its old tree mapped,
 * so the delete only succeeds after it has restarted). Failures are ignored:
 * this is housekeeping, never a precondition for an install.
 */
function sweepParked(root) {
  const dir = path.join(root, ".runtime");
  let entries;
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    if (!name.startsWith(path.basename(PREVIOUS_REL))) continue;
    try {
      fs.rmSync(path.join(dir, name), { recursive: true, force: true });
    } catch {
      /* still mapped; the next run tries again */
    }
  }
}

/**
 * Put the tree the app was already using back, after a post-swap step failed.
 * A new tree without the generated Prisma client (or without the python stack)
 * boots into a worse state than the one we started from, so a failed
 * `postinstall` is a rollback, not a warning.
 */
function restorePrevious(root, previous, { log = () => {} } = {}) {
  if (!previous || !fs.existsSync(previous)) return false;
  const live = path.join(root, "node_modules");
  const failed = path.join(root, ".runtime", `deps-failed-${Date.now()}`);
  try {
    if (fs.existsSync(live)) fs.renameSync(live, failed);
    fs.renameSync(previous, live);
    log("restored the previous dependency tree");
  } catch (err) {
    log(`could not restore the previous dependency tree: ${err && err.message ? err.message : err}`);
    return false;
  }
  try {
    fs.rmSync(failed, { recursive: true, force: true });
  } catch {
    /* still mapped by the running app; swept by the next install */
  }
  return true;
}

/**
 * Install dependencies without ever emptying the live tree.
 * Returns `{ previous, scripts }`; throws (with a bootable tree in place) on
 * failure — the old tree if the install or a deferred script failed.
 */
function installDependencies(root, { log = () => {}, env = process.env, runInstall, runScript } = {}) {
  // Free disk from earlier runs before adding a second tree.
  sweepParked(root);
  const modules = stageDependencies(root, { log, env, runInstall });
  const { previous } = swapIntoPlace(root, modules, { log });
  log("dependency tree swapped in");
  // Nothing but the emptied stage shell is left behind; the tree it held is now
  // the live one.
  try {
    fs.rmSync(path.join(root, STAGE_REL), { recursive: true, force: true });
  } catch {
    /* an antivirus scan may hold it; the next run clears it */
  }

  const scripts = deferredScripts(root);
  const run = runScript || defaultRunScript;
  for (const name of scripts) {
    log(`running npm run ${name}…`);
    try {
      run(root, name, env);
    } catch (err) {
      const restored = restorePrevious(root, previous, { log });
      throw new Error(
        `\`npm run ${name}\` failed after the install: ${err && err.message ? err.message : err}. ` +
          (restored
            ? "The previous dependency tree was put back — the app is exactly as it was."
            : "The new tree is in place but incomplete — run `npm install` by hand."),
      );
    }
  }

  dropPrevious(root, { log });
  return { previous, scripts };
}

module.exports = {
  STAGE_REL,
  PREVIOUS_REL,
  installDependencies,
  stageDependencies,
  swapIntoPlace,
  dropPrevious,
  sweepParked,
  restorePrevious,
  stagedManifest,
  deferredScripts,
  assertUsableTree,
};
