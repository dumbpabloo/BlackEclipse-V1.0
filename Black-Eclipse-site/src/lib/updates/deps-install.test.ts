import { afterEach, beforeEach, describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";

// The updater is CommonJS (`scripts/lib/deps-install.js`) because it runs under
// plain node in a spawned child, not through the Next build. Its contract is the
// one thing that must never regress: a failed dependency install leaves the live
// `node_modules` byte-for-byte intact.
const require_ = createRequire(import.meta.url);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const deps: any = require_("../../../scripts/lib/deps-install.js");

let root: string;

const manifest = {
  name: "black-eclipse",
  version: "0.4.26",
  dependencies: { next: "15.0.0", react: "19.0.0", "react-dom": "19.0.0" },
  devDependencies: { vitest: "3.0.0" },
  scripts: {
    build: "next build",
    postinstall: "prisma generate && node scripts/setup-python.js",
    prepare: "husky install",
    test: "vitest run",
  },
};

/** A directory that looks like a complete `npm ci` result. */
function fakeInstall(cwd: string) {
  const modules = path.join(cwd, "node_modules");
  for (const name of ["next", "react", "react-dom"]) {
    fs.mkdirSync(path.join(modules, name), { recursive: true });
    fs.writeFileSync(path.join(modules, name, "package.json"), JSON.stringify({ name }));
  }
  fs.writeFileSync(path.join(modules, ".package-lock.json"), "{}");
}

function liveTree(root_: string, marker: string) {
  const modules = path.join(root_, "node_modules");
  fs.mkdirSync(path.join(modules, "react"), { recursive: true });
  fs.writeFileSync(path.join(modules, "react", "package.json"), JSON.stringify({ marker }));
  fs.writeFileSync(path.join(modules, ".package-lock.json"), "{}");
  return modules;
}

function markerOf(modules: string) {
  const pkg = JSON.parse(fs.readFileSync(path.join(modules, "react", "package.json"), "utf8"));
  return pkg.marker;
}

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "be-deps-"));
  fs.writeFileSync(path.join(root, "package.json"), JSON.stringify(manifest, null, 2));
  fs.writeFileSync(path.join(root, "package-lock.json"), "{}");
});

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true });
});

describe("stagedManifest", () => {
  it("keeps dependencies and drops the project-only lifecycle scripts", () => {
    const staged = deps.stagedManifest(root);
    expect(staged.dependencies).toEqual(manifest.dependencies);
    expect(staged.devDependencies).toEqual(manifest.devDependencies);
    expect(staged.scripts).toEqual({ build: "next build", test: "vitest run" });
  });

  it("reports the scripts that must run after the swap, in npm's own order", () => {
    // preinstall → install → postinstall → prepare is the order npm itself uses.
    expect(deps.deferredScripts(root)).toEqual(["postinstall", "prepare"]);
  });
});

describe("installDependencies", () => {
  it("swaps a verified tree into place and runs the deferred scripts there", () => {
    const live = liveTree(root, "old");
    const ran: string[] = [];

    deps.installDependencies(root, {
      runInstall: (cwd: string) => fakeInstall(cwd),
      runScript: (cwd: string, name: string) => {
        ran.push(`${path.basename(cwd)}:${name}`);
      },
      log: () => {},
    });

    expect(markerOf(live)).toBeUndefined(); // fresh tree, not the old marker
    expect(fs.existsSync(path.join(live, "next", "package.json"))).toBe(true);
    expect(ran).toEqual([`${path.basename(root)}:postinstall`, `${path.basename(root)}:prepare`]);
  });

  it("leaves the live tree untouched when npm fails", () => {
    const live = liveTree(root, "keep-me");

    expect(() =>
      deps.installDependencies(root, {
        runInstall: () => {
          throw new Error("EPERM: operation not permitted, unlink next-swc.node");
        },
        runScript: () => {},
        log: () => {},
      }),
    ).toThrow(/EPERM/);

    expect(markerOf(live)).toBe("keep-me");
    expect(fs.existsSync(path.join(root, "node_modules"))).toBe(true);
  });

  it("refuses a staged tree that is missing packages even when npm exits 0", () => {
    const live = liveTree(root, "keep-me");

    expect(() =>
      deps.installDependencies(root, {
        // npm "succeeded" but produced an empty node_modules.
        runInstall: (cwd: string) => fs.mkdirSync(path.join(cwd, "node_modules"), { recursive: true }),
        runScript: () => {},
        log: () => {},
      }),
    ).toThrow(/missing|lockfile/);

    expect(markerOf(live)).toBe("keep-me");
  });

  it("restores the previous tree when postinstall fails", () => {
    const live = liveTree(root, "old");

    expect(() =>
      deps.installDependencies(root, {
        runInstall: (cwd: string) => fakeInstall(cwd),
        runScript: () => {
          throw new Error("prisma generate failed");
        },
        log: () => {},
      }),
    ).toThrow(/prisma generate failed/);

    // A tree without a generated Prisma client must never be what the user
    // boots into: the old tree is put back and still carries its marker.
    expect(markerOf(live)).toBe("old");
    expect(fs.existsSync(path.join(live, "next", "package.json"))).toBe(false);
  });
});

describe("swapIntoPlace", () => {
  it("restores the live tree when the staged move fails", () => {
    const live = liveTree(root, "live");
    const staged = path.join(root, ".runtime", "deps-stage", "node_modules");
    fs.mkdirSync(staged, { recursive: true });
    fs.writeFileSync(path.join(staged, ".package-lock.json"), "{}");

    // A file, not a directory: the second rename cannot succeed.
    fs.mkdirSync(path.join(root, "blocked"));
    const original = fs.renameSync;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (fs as any).renameSync = (from: string, to: string) => {
      if (from === staged && to === live) throw new Error("target exists");
      return original(from, to);
    };
    try {
      expect(() => deps.swapIntoPlace(root, staged, { log: () => {} })).toThrow(/could not be moved/);
    } finally {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (fs as any).renameSync = original;
    }

    expect(fs.existsSync(live)).toBe(true);
    expect(markerOf(live)).toBe("live");
  });
});
