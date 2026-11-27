import { NextResponse } from "next/server";
import { HARNESS_PY_URL, ensurePythonRuntime } from "@/lib/harness/python-runtime";

export const dynamic = "force-dynamic";

// Public webhook door for a WEBHOOK bot. Deliberately not operator-gated: the
// caller is an external service. Authorization is the per-bot bearer token the
// Python runtime compares in constant time. This route only forwards; it never
// decides whether the call is allowed.
export async function POST(
  request: Request,
  ctx: { params: Promise<{ id: string }> },
) {
  const { id } = await ctx.params;
  if (!id) return NextResponse.json({ error: "missing bot id" }, { status: 400 });

  const token = request.headers.get("x-eclipse-webhook-token")
    ?? new URL(request.url).searchParams.get("token")
    ?? "";
  if (!token) return NextResponse.json({ error: "missing webhook token" }, { status: 403 });

  try {
    if (!(await ensurePythonRuntime())) {
      return NextResponse.json({ error: "runtime unavailable" }, { status: 503 });
    }
  } catch {
    return NextResponse.json({ error: "runtime unavailable" }, { status: 503 });
  }

  const body = await request.text();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "x-eclipse-webhook-token": token,
  };
  const requestId = request.headers.get("x-request-id");
  if (requestId) headers["x-request-id"] = requestId;

  try {
    const res = await fetch(
      `${HARNESS_PY_URL}/automations/webhook/${encodeURIComponent(id)}`,
      {
        method: "POST",
        headers,
        body: body || "{}",
        cache: "no-store",
        signal: AbortSignal.timeout(30_000),
      },
    );
    const text = await res.text();
    return new NextResponse(text, {
      status: res.status,
      headers: { "Content-Type": res.headers.get("content-type") ?? "application/json" },
    });
  } catch {
    return NextResponse.json({ error: "runtime unavailable" }, { status: 503 });
  }
}
