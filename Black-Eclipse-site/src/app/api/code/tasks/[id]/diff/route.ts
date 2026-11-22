import { NextResponse } from "next/server";
import { HARNESS_PY_URL, ensurePythonRuntime } from "@/lib/harness/python-runtime";
import { runtimeCallbackToken } from "@/lib/harness/runtime-auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const incoming = new URL(request.url).searchParams;
  const mode = incoming.get("mode") ?? "working";
  const base = incoming.get("base") ?? "";
  if (!(await ensurePythonRuntime())) {
    return NextResponse.json({ error: "runtime unavailable" }, { status: 503 });
  }
  const token = runtimeCallbackToken();
  const query = new URLSearchParams({ mode });
  if (base) query.set("base", base);
  const res = await fetch(
    `${HARNESS_PY_URL}/code/tasks/${encodeURIComponent(id)}/diff?${query.toString()}`,
    {
      method: "GET",
      headers: token ? { "X-Harness-Operator": token } : undefined,
      signal: AbortSignal.timeout(30_000),
    },
  );
  const body = await res.json().catch(() => ({}));
  return NextResponse.json(body, { status: res.status });
}
