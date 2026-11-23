import path from "node:path";
import { spawn } from "node:child_process";
import { NextRequest, NextResponse } from "next/server";
import packageJson from "../../../../../package.json";
import { guardExpensiveMutation } from "@/lib/api-guard";
import { getUpdateStatus, isSourceCheckout } from "@/lib/updates/core";
import { readUpdateJob, updateJobFile } from "@/lib/updates/job";
import { isUpdateChannel, type UpdateChannel } from "@/lib/updates/types";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const ROOT = process.cwd();
const RUNNER = path.join(ROOT, "scripts", "update-apply.js");

/** GET /api/updates/apply — the staged-update job, if any. */
export async function GET() {
  return NextResponse.json(
    { job: readUpdateJob({ installedVersion: packageJson.version }) },
    { headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" } },
  );
}

/**
 * POST /api/updates/apply — stage the available signed release.
 *
 * This does NOT apply anything to the running window. It starts the same
 * verified updater the CLI uses, in its build-only mode: signature check, DB
 * backup, `git checkout <tag>`, `npm ci`, `npm run build`. Promoting the build
 * restarts the window, so that step is a separate, user-confirmed action owned
 * by the desktop shell (see src/lib/desktop/update-bridge.ts).
 *
 * Refusals are explicit: no manifest, not published, nothing newer, or a run
 * already in flight all return a message the UI can show verbatim.
 */
export async function POST(request: NextRequest) {
  const blocked = guardExpensiveMutation(request, "update-apply", {
    limit: 3,
    windowMs: 60_000,
  });
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });

  const requested = request.nextUrl.searchParams.get("channel") || "stable";
  const channel: UpdateChannel = isUpdateChannel(requested) ? requested : "stable";

  // `npm run build` always produces `.next` (the Stable channel's build dir), so
  // staging from the Dev window would promote the wrong app. Refuse honestly.
  if (process.env.BE_DESKTOP_CHANNEL === "dev") {
    return NextResponse.json(
      { error: "In-app updates are available in the Stable app only. Run `black update` here." },
      { status: 409 },
    );
  }

  const existing = readUpdateJob({ installedVersion: packageJson.version });
  if (existing && existing.status === "running") {
    return NextResponse.json(
      { error: "An update is already in progress.", job: existing },
      { status: 409 },
    );
  }

  const currentVersion = packageJson.version;
  const status = await getUpdateStatus({
    currentVersion,
    channel,
    refresh: true,
  });
  if (!status.available || !status.latestVersion) {
    return NextResponse.json(
      { error: status.error || "No update is available to install.", job: existing },
      { status: 409 },
    );
  }

  // A packaged install has no git checkout for the verified updater to move.
  // Refuse honestly instead of spawning a runner that can only fail.
  if (!isSourceCheckout()) {
    return NextResponse.json(
      {
        error:
          `This installed build cannot update in place. Download v${status.latestVersion} ` +
          "from the release page and run the installer.",
      },
      { status: 409 },
    );
  }

  try {
    const child = spawn(process.execPath, [RUNNER, "--channel", channel], {
      cwd: ROOT,
      detached: true,
      windowsHide: true,
      stdio: "ignore",
      env: {
        ...process.env,
        BE_UPDATE_JOB_FILE: updateJobFile(),
        // The version this window actually serves (from the signed release it
        // was installed from), not the checkout on disk. The runner must compare
        // against that, or a checkout ahead of the pinned UI makes it report
        // "nothing to install" forever — and a stale baked version makes it
        // offer the same update repeatedly.
        BE_UPDATE_RUNNING_VERSION: currentVersion,
      },
    });
    child.unref();
  } catch (err) {
    return NextResponse.json(
      {
        error: `Could not start the updater: ${err instanceof Error ? err.message : "unknown error"}`,
      },
      { status: 500 },
    );
  }

  // The runner writes its first heartbeat immediately; give it a moment so the
  // response already carries a job id the UI can poll (instead of "null").
  const job = await waitForJob(1200);
  return NextResponse.json({ ok: true, job }, { status: 202 });
}

async function waitForJob(timeoutMs: number) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const job = readUpdateJob({ installedVersion: packageJson.version });
    if (job) return job;
    if (Date.now() >= deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}
