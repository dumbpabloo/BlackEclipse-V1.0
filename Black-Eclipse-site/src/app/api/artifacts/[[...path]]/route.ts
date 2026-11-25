// ─── Artifact Center API proxy ───────────────────────────────────────────────
// A thin pass-through to the Python runtime's /artifacts routes, so the UI and
// the agent reach the SAME durable artifact catalog. No artifact logic lives
// here. Mutations pass the shared CSRF/origin guard; operator authorization is
// by transport (loopback) or the runtime token, presented to Python as a header.

import { NextResponse } from "next/server";
import { requireApiAuth } from "@/lib/auth/server";
import { HARNESS_PY_URL, ensurePythonRuntime } from "@/lib/harness/python-runtime";
import { runtimeCallbackToken } from "@/lib/harness/runtime-auth";
import { runtimeIdentityHeaders } from "@/lib/harness/runtime-identity";
import { guardMutation } from "@/lib/api-guard";

export const dynamic = "force-dynamic";

type Ctx = { params: Promise<{ path?: string[] }> };

async function proxy(request: Request, ctx: Ctx, method: string): Promise<Response> {
  const denied = await requireApiAuth(request);
  if (denied) return denied;
  if (method !== "GET") {
    const blocked = guardMutation(request, "artifacts");
    if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });
  }
  const healthy = await ensurePythonRuntime();
  if (!healthy) {
    return NextResponse.json(
      { error: "The Python agent runtime is not available. Install the runtime: npm run setup:python" },
      { status: 503 },
    );
  }
  const { path } = await ctx.params;
  const parts = path ?? [];
  // Reject traversal before building the upstream path, and encode each segment
  // so a segment can never introduce an extra path component. Without this the
  // catch-all could reach a sibling runtime route while carrying the operator
  // token.
  if (parts.some((p) => p === "." || p === ".." || p.includes("\\"))) {
    return NextResponse.json({ error: "invalid artifact path" }, { status: 400 });
  }
  const segments = parts.map(encodeURIComponent).join("/");
  const target = segments === "" ? "/artifacts" : `/artifacts/${segments}`;
  const url = new URL(request.url);
  const token = runtimeCallbackToken();
  // Ownership is NEVER taken from a caller-supplied header. The verified
  // identity JWT (`X-Harness-Identity`) is minted by this front door from the
  // signed session; Python resolves the owner from it. A browser header such as
  // `x-eclipse-user` is attacker-controlled and used to select another user's
  // artifacts, so it is no longer forwarded.
  // A download stream (video/audio playback) is bound by the CLIENT's
  // connection, not by a fixed proxy timeout — aborting a large MP4 at 60 s
  // would cut long playback off mid-stream. JSON calls keep the hard bound.
  const isDownload = segments.endsWith("/download");
  const range = request.headers.get("range");
  const ifRange = request.headers.get("if-range");
  const init: RequestInit = {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Harness-Operator": token } : {}),
      ...(await runtimeIdentityHeaders(request)),
      ...(range ? { Range: range } : {}),
      ...(ifRange ? { "If-Range": ifRange } : {}),
    },
    signal: isDownload ? request.signal : AbortSignal.timeout(60_000),
  };
  if (method !== "GET" && method !== "DELETE") {
    init.body = await request.text();
  }
  try {
    // A media stream is bound by the client connection; one transient upstream
    // failure (a runtime restart, a closed socket) must not end playback. JSON
    // calls keep their single attempt: a retried mutation is not a retry.
    const upstream = async (): Promise<Response> => {
      try {
        return await fetch(`${HARNESS_PY_URL}${target}${url.search}`, init);
      } catch (error) {
        const signal = init.signal as AbortSignal | undefined;
        if (method !== "GET" || request.signal.aborted || signal?.aborted) throw error;
        return await fetch(`${HARNESS_PY_URL}${target}${url.search}`, init);
      }
    };
    const res = await upstream();
    // Downloads stream bytes, not JSON — pass them through untouched.
    const contentType = res.headers.get("content-type") ?? "";
    if (method === "GET" && !contentType.includes("application/json")) {
      // Pass the byte-stream metadata through untouched — media players need
      // the range headers to seek (Accept-Ranges/Content-Range/206).
      const headers: Record<string, string> = {
        "Content-Type": contentType || "application/octet-stream",
        Vary: "Range",
      };
      for (const name of [
        "content-disposition",
        "cache-control",
        "accept-ranges",
        "content-range",
        "content-length",
        "etag",
      ]) {
        const value = res.headers.get(name);
        if (value) headers[name] = value;
      }
      return new NextResponse(res.body, { status: res.status, headers });
    }
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "artifact request failed" },
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
export async function PATCH(request: Request, ctx: Ctx) {
  return proxy(request, ctx, "PATCH");
}
export async function DELETE(request: Request, ctx: Ctx) {
  return proxy(request, ctx, "DELETE");
}
