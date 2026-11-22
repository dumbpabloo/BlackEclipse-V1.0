import { NextResponse } from "next/server";
import { autolandTask } from "@/lib/harness/clients/code-client";
import { HarnessRuntimeUnavailable } from "@/lib/harness";

export const dynamic = "force-dynamic";

export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const full = new URL(request.url).searchParams.get("full") === "true";
  try {
    const result = await autolandTask(id, full);
    return NextResponse.json(result);
  } catch (err) {
    const unavailable = err instanceof HarnessRuntimeUnavailable;
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "autoland failed" },
      { status: unavailable ? 503 : 409 },
    );
  }
}
