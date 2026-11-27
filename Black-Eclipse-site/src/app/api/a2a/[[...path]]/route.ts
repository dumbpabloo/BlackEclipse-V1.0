// ─── A2A (agent-to-agent) API proxy ───────────────────────────────────────────
// A thin pass-through to the Python runtime's /a2a routes, so the Bots surface
// can show the SAME durable teammate messages the runtime uses. No messaging
// logic lives here. Mutations pass the shared CSRF/origin guard; operator
// authorization is by transport (loopback) or the runtime token.
import { NextResponse } from "next/server";
import { HARNESS_PY_URL, ensurePythonRuntime } from "@/lib/harness/python-runtime";
import { runtimeCallbackToken } from "@/lib/harness/runtime-auth";
import { runtimeIdentityHeaders } from "@/lib/harness/runtime-identity";
import { guardMutation } from "@/lib/api-guard";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path?: string[] }> };

async function proxy(request: Request, ctx: Ctx, method: string): Promise<NextResponse> {
  if (method !== "GET") {
    const blocked = guardMutation(request, "a2a");
    if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });
  }
  if (!(await ensurePythonRuntime())) {
    return NextResponse.json({ error: "runtime unavailable" }, { status: 503 });
  }
  const { path } = await ctx.params;
  const segments = (path ?? []).join("/");
  const target = segments === "" ? "/a2a" : `/a2a/${segments}`;
  const url = new URL(request.url);
  const token = runtimeCallbackToken();
  const init: RequestInit = {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Harness-Operator": token } : {}),
      ...(await runtimeIdentityHeaders(request)),
    },
    signal: AbortSignal.timeout(30_000),
  };
  if (method !== "GET" && method !== "DELETE") init.body = await request.text();
  try {
    const res = await fetch(`${HARNESS_PY_URL}${target}${url.search}`, init);
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "a2a request failed" },
      { status: 502 },
    );
  }
}

export async function GET(request: Request, ctx: Ctx) {
  return proxy(request, ctx, "GET");
}
export async function POST(request: Request, ctx: Ctx) {
  return proxy(request, ctx, "POST");
}
export async function DELETE(request: Request, ctx: Ctx) {
  return proxy(request, ctx, "DELETE");
}
