import { NextResponse } from "next/server";
import { guardMutation } from "@/lib/api-guard";
import { listCodeTasks, createCodeTask, type Autonomy } from "@/lib/harness/clients/code-client";
import { resolveRunModelSettings } from "@/lib/harness/run-service";
import { runtimeBaseUrl, runtimeCallbackToken } from "@/lib/harness/runtime-auth";
import { HarnessRuntimeUnavailable } from "@/lib/harness";

export const dynamic = "force-dynamic";

const AUTONOMY: Autonomy[] = ["plan_only", "edit_verify", "approve_external"];
const BUDGETS = ["small", "standard", "deep"] as const;

export async function GET() {
  try {
    const tasks = await listCodeTasks();
    return NextResponse.json({ success: true, tasks });
  } catch (err) {
    const unavailable = err instanceof HarnessRuntimeUnavailable;
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "task list failed" },
      { status: unavailable ? 503 : 500 },
    );
  }
}

export async function POST(request: Request) {
  const blocked = guardMutation(request, "code-task-create");
  if (blocked) return NextResponse.json(blocked.body, { status: blocked.status });
  try {
    const body = (await request.json().catch(() => ({}))) as {
      query?: string;
      autonomy?: string;
      workspaceId?: string;
      workspaceRootId?: string;
      modelSettings?: Record<string, unknown>;
      callbackBaseUrl?: string;
      callbackToken?: string;
      direct?: boolean;
      budgetProfile?: string;
      acceptance?: string[];
      images?: import("@/lib/harness/types").RunImageInput[];
    };
    if (typeof body.query !== "string" || !body.query.trim()) {
      return NextResponse.json({ error: "query is required" }, { status: 400 });
    }
    const autonomy = (body.autonomy ?? "plan_only") as Autonomy;
    if (!AUTONOMY.includes(autonomy)) {
      return NextResponse.json({ error: "unknown autonomy" }, { status: 400 });
    }
    const budgetProfile = body.budgetProfile ?? "standard";
    if (!(BUDGETS as readonly string[]).includes(budgetProfile)) {
      return NextResponse.json({ error: "unknown budget profile" }, { status: 400 });
    }
    if (body.acceptance !== undefined && !Array.isArray(body.acceptance)) {
      return NextResponse.json({ error: "acceptance must be a list" }, { status: 400 });
    }
    // A UI "Build this" action has no vault access, so the model settings are
    // resolved server-side (the same one resolution path a chat run uses). The
    // runtime binds its own server-resolved code root; no workspace ref is sent.
    const modelSettings =
      body.modelSettings && Object.keys(body.modelSettings).length
        ? body.modelSettings
        : await resolveRunModelSettings(undefined, "code");
    const task = await createCodeTask({
      query: body.query.trim(),
      autonomy,
      workspaceId: body.workspaceId,
      workspaceRootId: body.workspaceRootId,
      modelSettings,
      // The runtime requires a callback base URL + token to reach the front door
      // (data bridge, approvals). The browser has no vault access, so the front
      // door supplies them server-side — the same values a chat run uses.
      callbackBaseUrl: body.callbackBaseUrl ?? runtimeBaseUrl(),
      callbackToken: body.callbackToken ?? runtimeCallbackToken(),
      direct: body.direct === true,
      budgetProfile: budgetProfile as "small" | "standard" | "deep",
      acceptance: (body.acceptance ?? []).filter((item) => typeof item === "string").slice(0, 20),
      images: Array.isArray(body.images) ? body.images.slice(0, 4) : undefined,
    });
    return NextResponse.json({ success: true, task });
  } catch (err) {
    return NextResponse.json({ error: err instanceof Error ? err.message : "task create failed" }, { status: 500 });
  }
}
