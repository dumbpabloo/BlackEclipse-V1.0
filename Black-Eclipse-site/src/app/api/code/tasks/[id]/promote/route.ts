import { NextResponse } from "next/server";
import { applyPromotion } from "@/lib/harness/clients/code-client";
import { HarnessRuntimeUnavailable } from "@/lib/harness";

export const dynamic = "force-dynamic";

export async function POST(request: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const body = (await request.json().catch(() => ({}))) as {
    approvalId?: string;
    previewDigest?: string;
    include?: string[];
    hunks?: string[];
  };
  if (!body.approvalId || !body.previewDigest) {
    return NextResponse.json({ error: "approvalId and previewDigest are required" }, { status: 400 });
  }
  try {
    const promotion = await applyPromotion(id, body.approvalId, body.previewDigest, body.include ?? [], body.hunks ?? []);
    return NextResponse.json({ success: true, promotion });
  } catch (err) {
    const unavailable = err instanceof HarnessRuntimeUnavailable;
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "promotion failed" },
      { status: unavailable ? 503 : 409 },
    );
  }
}
