import { NextResponse } from "next/server";
import { prisma } from "@/lib/db";
import { listAutomationAudit, listAutomations } from "@/lib/harness/clients/automation-client";

export const dynamic = "force-dynamic";

// Privacy-safe bot diagnostics: configuration shape and counts, never
// transcripts, workspace paths, webhook tokens, credential refs or provider keys.
export async function GET() {
  try {
    const [automations, audit, rooms, chatMessages, groupMessages] = await Promise.all([
      listAutomations(),
      listAutomationAudit(undefined, 200),
      prisma.botRoom.findMany({ orderBy: { createdAt: "asc" } }),
      prisma.botChatMessage.count(),
      prisma.botGroupMessage.count(),
    ]);
    const bots = automations.map((bot) => {
      const config = bot.action.config ?? {};
      const model = (config.modelSettings as { model?: unknown } | undefined)?.model;
      const backup = (config.backupModelSettings as { model?: unknown } | undefined)?.model;
      return {
        id: bot.id,
        name: bot.name,
        state: bot.state,
        version: bot.version,
        triggerType: bot.trigger.type,
        actionType: bot.action.type,
        agentId: bot.action.agentId,
        model: typeof model === "string" ? model : null,
        backupModel: typeof backup === "string" ? backup : null,
        hasWorkspace: Boolean(bot.action.workspaceRoot),
        tags: bot.tags,
      };
    });
    const roomRows = rooms.map((room) => {
      let memberCount = 0;
      try {
        const parsed = JSON.parse(room.memberIds) as unknown;
        if (Array.isArray(parsed)) memberCount = parsed.length;
      } catch {
        memberCount = 0;
      }
      return {
        id: room.id,
        name: room.name,
        mode: room.mode,
        routingMode: room.routingMode,
        hasLead: Boolean(room.leadId),
        memberCount,
      };
    });
    return NextResponse.json({
      success: true,
      diagnostics: {
        format: "eclipse.bots.diagnostics",
        version: 1,
        generatedAt: new Date().toISOString(),
        counts: {
          bots: bots.length,
          rooms: roomRows.length,
          chatMessages,
          groupMessages,
          auditEvents: audit.length,
        },
        bots,
        rooms: roomRows,
        audit: audit.map((event) => ({
          id: event.id,
          automationId: event.automationId,
          action: event.action,
          actor: event.actor,
          createdAt: event.createdAt,
        })),
      },
    });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "failed to build diagnostics" },
      { status: 500 },
    );
  }
}
