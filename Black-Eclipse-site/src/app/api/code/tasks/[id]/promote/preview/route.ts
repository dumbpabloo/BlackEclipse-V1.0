import { NextResponse } from "next/server";
import { previewPromotion } from "@/lib/harness/clients/code-client";
import { HarnessRuntimeUnavailable } from "@/lib/harness";

export const dynamic = "force-dynamic";

export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = (await request.json().catch(() => ({}))) as { include?: string[]; hunks?: string[] };
  try {
    const result = await previewPromotion(id, body.include ?? [], body.hunks ?? []);
    return NextResponse.json({ success: true, ...result });
  } catch (err) {
    const unavailable = err instanceof HarnessRuntimeUnavailable;
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "preview failed" },
      { status: unavailable ? 503 : 400 },
    );
  }
}
