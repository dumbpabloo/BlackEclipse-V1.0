"use strict";

// Does the download button actually serve a file?
//
// The README links (and scripts/install-release.*) resolve
// `github.com/<repo>/releases/latest/download/<asset>`. That URL has three
// independent preconditions, and a 404 tells you nothing about which one broke:
//
//   1. the repository is publicly readable (a PRIVATE repo 404s anonymously,
//      exactly like a missing one - no file is ever served);
//   2. a release is PUBLISHED - `.github/workflows/desktop-release.yml` creates
//      releases as drafts, and `releases/latest` ignores drafts entirely, so a
//      green CI run is not a working button;
//   3. the asset name matches what the build produced.
//
// This probe separates those. It is deliberately NOT part of `check:all`: it
// needs the network, so it belongs in a pre-announce check, not the local gate.
//
//   npm run check:downloads
//   npm run check:downloads -- --version 0.4.0 --repo owner/name
//   npm run check:downloads -- --all          # include macOS/Linux (backfill)
//
// Exit 0 = every checked button serves a file. Exit 1 = at least one does not.

const DEFAULT_REPO = process.env.BLACK_ECLIPSE_REPO || "dumbpabloo/BlackEclipse-V1.0";

// The alias the README button points at. The release workflow asserts it exists
// before it publishes, so a name here that 404s on a public repo is a
// publication problem, not a naming one.
const ASSETS = ["BlackEclipse-Setup.exe"];

// Built on request, not published and not advertised (docs/install.md). Check
// them with `--all` after a backfill; a missing one is not a broken button.
const UNPUBLISHED_ASSETS = [
  "BlackEclipse-linux-amd64.deb",
  "BlackEclipse-mac-arm64.dmg",
];

const TIMEOUT_MS = 20000;

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1) return fallback;
  const value = process.argv[i + 1];
  if (!value || value.startsWith("--")) {
    console.error(`[downloads] --${name} needs a value.`);
    process.exit(2);
  }
  return value;
}

const repo = arg("repo", DEFAULT_REPO);
const version = arg("version", "latest");
const explicitAssets = arg("assets", "");
const assets = explicitAssets
  ? explicitAssets.split(",").map((a) => a.trim()).filter(Boolean)
  : process.argv.includes("--all")
    ? [...ASSETS, ...UNPUBLISHED_ASSETS]
    : ASSETS;

if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) {
  console.error(`[downloads] --repo must look like owner/name (got "${repo}").`);
  process.exit(2);
}

const tag = version === "latest" ? null : version.startsWith("v") ? version : `v${version}`;
const base = tag
  ? `https://github.com/${repo}/releases/download/${tag}`
  : `https://github.com/${repo}/releases/latest/download`;

function headers() {
  return { "user-agent": "black-eclipse-download-check", accept: "*/*" };
}

async function probe(url) {
  for (const method of ["HEAD", "GET"]) {
    try {
      const res = await fetch(url, {
        method,
        redirect: "follow",
        headers: headers(),
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      // A HEAD the CDN refuses says nothing about the file; retry as GET.
      if (method === "HEAD" && res.status >= 400) continue;
      return {
        ok: res.ok,
        status: res.status,
        size: Number(res.headers.get("content-length") || 0),
        finalUrl: res.url,
      };
    } catch (err) {
      if (method === "GET") return { ok: false, status: 0, size: 0, error: err.message };
    }
  }
  return { ok: false, status: 0, size: 0, error: "no response" };
}

async function github(path) {
  try {
    const res = await fetch(`https://api.github.com${path}`, {
      headers: { ...headers(), accept: "application/vnd.github+json" },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (res.status === 404) return { notFound: true };
    if (!res.ok) return { error: `HTTP ${res.status}` };
    return { data: await res.json() };
  } catch (err) {
    return { error: err.message };
  }
}

function size(bytes) {
  if (!bytes) return "";
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

async function explain(results) {
  const repoCheck = await github(`/repos/${repo}`);
  if (repoCheck.notFound) {
    console.log("");
    console.log(`[downloads] The repository "${repo}" is not publicly readable.`);
    console.log("[downloads] GitHub answers 404 for a private repo exactly as it does for a");
    console.log("[downloads] missing one, and serves no release files either way. Make the repo");
    console.log("[downloads] public, or host the installers somewhere anonymous access works.");
    return;
  }
  if (repoCheck.error) {
    console.log(`[downloads] Could not ask the GitHub API (${repoCheck.error}).`);
    return;
  }
  if (repoCheck.data.private) {
    console.log("[downloads] The repository is PRIVATE. No download URL can work for a user");
    console.log("[downloads] who is not signed in.");
    return;
  }

  const release = await github(
    tag ? `/repos/${repo}/releases/tags/${tag}` : `/repos/${repo}/releases/latest`,
  );
  if (release.notFound || !release.data) {
    console.log("");
    console.log(`[downloads] The repository is public, but no published release is visible${tag ? ` at ${tag}` : ""}.`);
    console.log("[downloads] The release workflow creates releases as DRAFTS (draft: true), and");
    console.log("[downloads] /releases/latest ignores both drafts and prereleases. Publish the draft");
    console.log("[downloads] on the releases page, or a green CI run still leaves the button dead.");
    return;
  }

  const have = (release.data.assets || []).map((a) => a.name);
  const missing = results.filter((r) => !r.ok).map((r) => r.asset);
  console.log("");
  console.log(`[downloads] Release ${release.data.tag_name} is published (draft: ${release.data.draft}).`);
  console.log(`[downloads] It carries ${have.length} asset(s)${have.length ? `: ${have.join(", ")}` : ""}.`);
  if (missing.length) {
    console.log(`[downloads] Missing from it: ${missing.join(", ")}.`);
    console.log("[downloads] A name change on the build side must update the README buttons and");
    console.log("[downloads] the release body in .github/workflows/desktop-release.yml together.");
  }
}

async function main() {
  console.log(`[downloads] repo:    ${repo}`);
  console.log(`[downloads] release: ${tag || "latest"}`);
  console.log(`[downloads] base:    ${base}`);

  const results = [];
  for (const asset of assets) {
    const outcome = await probe(`${base}/${asset}`);
    results.push({ asset, ...outcome });
    const detail = outcome.ok
      ? outcome.size
        ? size(outcome.size)
        : "served (no content-length)"
      : outcome.status
        ? `HTTP ${outcome.status}`
        : outcome.error || "failed";
    console.log(`[downloads] ${outcome.ok ? "ok  " : "FAIL"} ${asset}  (${detail})`);
  }

  // Windows is the only platform that publishes a checksum file today, so a 404
  // here is reported but not a failure. See docs/install.md.
  const sums = await probe(`${base}/SHA256SUMS.txt`);
  console.log(
    `[downloads] ${sums.ok ? "ok  " : "info"} SHA256SUMS.txt  (${sums.ok ? size(sums.size) : "not published - Windows-only today"})`,
  );

  const failed = results.filter((r) => !r.ok);
  if (failed.length === 0) {
    console.log("[downloads] All download buttons serve a file.");
    return 0;
  }
  await explain(results);
  console.log(`[downloads] ${failed.length} of ${assets.length} download button(s) are broken.`);
  return 1;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error(`[downloads] ${err.message}`);
    process.exit(1);
  });
