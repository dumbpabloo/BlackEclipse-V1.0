import { NextResponse } from "next/server";
import { HARNESS_PY_URL, ensurePythonRuntime } from "@/lib/harness/python-runtime";
import { runtimeCallbackToken } from "@/lib/harness/runtime-auth";

export const dynamic = "force-dynamic";

/** Bounded picture preview for the review diff. The Python runtime confines the
 *  path to the task's workspace and verifies the magic bytes. */
export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const path = new URL(request.url).searchParams.get("path") ?? "";
  if (!path) {
    return NextResponse.json({ error: "path is required" }, { status: 400 });
  }
  if (!(await ensurePythonRuntime())) {
    return NextResponse.json({ error: "runtime unavailable" }, { status: 503 });
  }
  const token = runtimeCallbackToken();
  const query = new URLSearchParams({ path });
  const res = await fetch(
    `${HARNESS_PY_URL}/code/tasks/${encodeURIComponent(id)}/image?${query.toString()}`,
    {
      method: "GET",
      headers: token ? { "X-Harness-Operator": token } : undefined,
      signal: AbortSignal.timeout(30_000),
    },
  );
  const body = await res.json().catch(() => ({}));
  return NextResponse.json(body, { status: res.status });
}
