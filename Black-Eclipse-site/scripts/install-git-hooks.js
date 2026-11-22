#!/usr/bin/env node
// Install the committed git hooks and enforce the correct commit identity.
//
// WHY THIS EXISTS: GitHub credits contributions by the author EMAIL in each
// commit. A local `git config user.email` that is not linked to the GitHub
// account produces commits that silently never count. This script makes the
// correct identity the default on every machine and installs the guard hooks,
// so the problem cannot recur. It is wired into `postinstall`, so a fresh clone
// is protected without any manual step.
//
// It never overwrites a repository-local email the user deliberately set to
// something else UNLESS it is the known-bad address; it always points
// core.hooksPath at the committed `.githooks` directory.

const { execFileSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const PKG_ROOT = path.join(__dirname, "..");

// The git work tree root may be ABOVE the package dir (this repo keeps the app
// in Black-Eclipse-site/ inside the git root). Resolve it from git itself so the
// hooks land where commits actually happen.
let ROOT = PKG_ROOT;
try {
  ROOT = execFileSync("git", ["rev-parse", "--show-toplevel"], {
    cwd: PKG_ROOT,
    encoding: "utf8",
  }).trim() || PKG_ROOT;
} catch {
  ROOT = PKG_ROOT;
}

const HOOKS_DIR = path.join(ROOT, ".githooks");

// The canonical identity for this repository. The noreply address is derived
// from the GitHub account id, so it is inherently linked to the account and
// always counts — unlike a personal email that may be unverified.
const CANONICAL_NAME = "dumbpabloo";
const CANONICAL_EMAIL = "121116805+dumbpabloo@users.noreply.github.com";
// Addresses known to produce uncredited commits. Replaced automatically.
// Keep personal addresses OUT of the repository: supply them locally instead,
// comma-separated, e.g. GIT_COMMIT_BAD_EMAILS="old@example.com,other@example.com".
const KNOWN_BAD_EMAILS = new Set([
  "user@blackeclipse.com",
  ...(process.env.GIT_COMMIT_BAD_EMAILS || "")
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean),
]);

function git(args, opts = {}) {
  return execFileSync("git", args, { cwd: ROOT, encoding: "utf8", ...opts }).trim();
}

function gitQuiet(args) {
  try {
    return { ok: true, out: git(args) };
  } catch (err) {
    return { ok: false, out: String(err.stderr || err.message || err) };
  }
}

function isGitRepo() {
  return gitQuiet(["rev-parse", "--is-inside-work-tree"]).ok;
}

function main() {
  if (!isGitRepo()) {
    console.log("[git-hooks] not a git work tree; skipping identity setup.");
    return;
  }

  // 1. Point git at the committed hooks directory (repo-local setting).
  if (!fs.existsSync(HOOKS_DIR)) {
    console.warn("[git-hooks] .githooks directory missing; skipping.");
    return;
  }
  git(["config", "core.hooksPath", ".githooks"]);
  console.log("[git-hooks] core.hooksPath -> .githooks");

  // 2. Enforce the canonical identity. Only rewrite when the current value is
  //    missing or a known-bad address, so a deliberate valid choice is respected.
  let currentEmail = "";
  const emailRead = gitQuiet(["config", "user.email"]);
  if (emailRead.ok) currentEmail = emailRead.out;

  const shouldSet =
    !currentEmail || KNOWN_BAD_EMAILS.has(currentEmail.toLowerCase());

  if (shouldSet) {
    git(["config", "user.name", CANONICAL_NAME]);
    git(["config", "user.email", CANONICAL_EMAIL]);
    console.log(
      `[git-hooks] set repo identity -> ${CANONICAL_NAME} <${CANONICAL_EMAIL}>` +
        (currentEmail ? ` (was ${currentEmail})` : "")
    );
  } else if (currentEmail !== CANONICAL_EMAIL) {
    // Leave a deliberate identity alone, but make the risk visible.
    try {
      gitQuiet(["config", "--global", "user.email"]);
      console.warn(
        `[git-hooks] note: user.email is ${currentEmail}, not the canonical ` +
          `${CANONICAL_EMAIL}. Ensure this email is verified on GitHub or your ` +
          `commits will not count as contributions. The pre-commit hook will block ` +
          `commits with an unapproved identity.`
      );
    } catch {
      /* best effort */
    }
  } else {
    console.log(`[git-hooks] identity already correct: ${currentEmail}`);
  }

  // 3. Make the hooks executable where the platform has exec bits (no-op on
  //    Windows). The identity guard (pre-commit), the CHANGELOG guard
  //    (commit-msg) and the protected-branch guard (pre-push) are committed in
  //    `.githooks/`.
  for (const name of ["pre-commit", "commit-msg", "pre-push"]) {
    const hook = path.join(HOOKS_DIR, name);
    if (fs.existsSync(hook)) {
      try {
        fs.chmodSync(hook, 0o755);
      } catch {
        /* Windows: not required */
      }
    }
  }
}

main();
