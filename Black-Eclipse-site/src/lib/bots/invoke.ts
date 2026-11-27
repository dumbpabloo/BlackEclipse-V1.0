// ── Bot / group-chat turn invoker (shared) ───────────────────────────────────
// The ONE place a bot-as-a-turn (or a group member-as-a-turn) is assembled and
// run. Lifted out of `bot-chat.tsx` and `group-chat-room.tsx` so the Bots
// workspace, the group rooms AND the main-chat `@` delegation all speak to the
// runtime exactly the same way — same persona, same history windowing, same
// private per-conversation memory scope and chat-id scheme. No second agent
// loop: this still goes through the one streaming agent provider.
//
// Callers own the transcript (their own store) and pass `onDelta`; this module
// never writes to a store.

'use client';

import { getAgentProvider } from '@/lib/agent/registry';
import type { ChatMessage } from '@/lib/agent/types';
import type { HarnessUiEvent } from '@/lib/agent/events';
import type { AutomationView } from '@/lib/harness/clients/automation-client';
import type { BotGroup } from '@/lib/bots/groups-store';
import { botModelPin } from '@/lib/bots/state';
import { visualTeachingPrompt, type StrategyVisual } from '@/lib/bots/trading';
import { referenceImages, tradingChatContext } from '@/lib/bots/trading/chat-context';
import { getGraphRun, isTerminal, startWorkflowRun } from '@/lib/graph-studio/execution-client';

/** Prior bot-chat turns. `error` marks a failed turn, excluded from context. */
export interface PriorBotTurn {
  role: 'user' | 'assistant';
  content: string;
  error?: boolean;
}

/** Prior group-room turns. `authorName` labels who spoke (member replies only). */
export interface PriorGroupTurn {
  role: 'user' | 'assistant';
  content: string;
  authorName?: string;
  error?: boolean;
}

/** The chat id whose PRIVATE per-conversation memory a bot's turns belong to. */
export function botChatId(botId: string): string {
  return `bot-${botId}`;
}

/** The chat id whose PRIVATE per-conversation memory a group member's turns belong to. */
export function groupMemberChatId(groupId: string, botId: string): string {
  return `group-${groupId}-${botId}`;
}

/** The server-owned workspace root for this bot's chat and scheduled runs. */
export function botWorkspaceRootId(botId: string): string {
  return `bot:${botId}`;
}

/** The standing identity/instructions line for a bot answering as itself. */
export function botPersona(bot: AutomationView): string {
  const instructions = bot.action.objective ?? bot.description ?? '';
  return (
    `You are the persistent bot "${bot.name}" (id: ${bot.id}). ` +
    `Purpose: ${bot.description || '—'}. Standing instructions: ${instructions || '—'}. ` +
    `Answer as this bot, and use your tools to act when the user asks.` +
    tradingPersonaContext(bot)
  );
}

/**
 * What a trading bot carries into every answer: its mode/authority, and the
 * operator's reference images as PATTERN GUIDANCE (never market data).
 */
export function tradingPersonaContext(bot: AutomationView): string {
  const config = bot.action.config ?? {};
  const visuals = Array.isArray(config.visuals)
    ? (config.visuals as StrategyVisual[])
    : [];
  const lines: string[] = [];
  const mode = typeof config.mode === 'string' ? config.mode : '';
  if (mode) lines.push(`Trading mode: ${mode}.`);
  const tier = bot.executionAuthority?.tier;
  if (tier) {
    lines.push(
      `Trading authority: ${tier.toUpperCase()} ` +
        (tier === 'l0'
          ? '(propose orders only; never claim an order was filled).'
          : tier === 'l1'
            ? '(you may fill a demo account; never claim live execution).'
            : '(you may fill live under a bot-pinned mandate; the broker is the source of truth).'),
    );
  }
  const teaching = visualTeachingPrompt(visuals);
  if (teaching) lines.push(teaching);
  return lines.length ? `\n\n${lines.join('\n')}` : '';
}

/** The standing identity/instructions line for one member inside a group room. */
export function groupMemberPersona(group: BotGroup, bot: AutomationView): string {
  return (
    `You are "${bot.name}", one member of the group chat "${group.name}". ` +
    `Your role: ${bot.description || '—'}. Your standing instructions: ${bot.action.objective || '—'}. ` +
    `Reply ONLY as ${bot.name}, in your own distinct voice. Do not speak for other ` +
    `members and do not summarize the room. Use your tools when asked.` +
    tradingPersonaContext(bot)
  );
}

/** Build the message list for a bot-as-a-turn: persona, ack, then recent turns. */
export function buildBotMessages(
  bot: AutomationView,
  text: string,
  prior: readonly PriorBotTurn[],
  limit = 10,
): ChatMessage[] {
  const history: ChatMessage[] = [
    { role: 'user', content: botPersona(bot) },
    { role: 'assistant', content: 'Understood — I am this bot.' },
    ...prior
      .filter((m) => !m.error && m.content.trim().length > 0)
      .slice(-limit)
      .map((m) => ({ role: m.role, content: m.content })),
  ];
  return [...history, { role: 'user', content: text }];
}

/** Build the message list for one group member's turn (prior turns author-labelled). */
export function buildGroupMemberMessages(
  group: BotGroup,
  bot: AutomationView,
  text: string,
  prior: readonly PriorGroupTurn[],
  limit = 8,
  contextPacket?: string,
): ChatMessage[] {
  const history: ChatMessage[] = [
    { role: 'user', content: groupMemberPersona(group, bot) },
    { role: 'assistant', content: `Understood — I am ${bot.name}.` },
    ...(contextPacket
      ? [{ role: 'system' as const, content: `Shared room context (bounded, unverified):\n${contextPacket}` }]
      : []),
    ...prior
      .filter((m) => !m.error && m.content.trim().length > 0)
      .slice(-limit)
      .map((m) => ({
        role: m.role,
        content: m.role === 'assistant' && m.authorName ? `${m.authorName}: ${m.content}` : m.content,
      })),
  ];
  return [...history, { role: 'user', content: text }];
}

export interface RunBotTurnArgs {
  bot: AutomationView;
  text: string;
  prior: readonly PriorBotTurn[];
  model?: string;
  signal?: AbortSignal;
  onDelta?: (chunk: string) => void;
  /** Typed harness events for this turn (approvals etc.), when the caller needs them. */
  onEvent?: (event: HarnessUiEvent) => void;
}

/**
 * Run one turn as the bot and resolve its final reply text. The caller owns the
 * placeholder message and the transcript; it streams `onDelta` into it.
 */
function formatWorkflowOutput(output: unknown): string {
  if (output === null || output === undefined) {
    return 'The workflow finished without a terminal output.';
  }
  if (typeof output === 'string') return output;
  if (typeof output === 'object' && !Array.isArray(output)) {
    const text = (output as { text?: unknown; message?: unknown }).text
      ?? (output as { message?: unknown }).message;
    if (typeof text === 'string' && text.trim()) return text;
  }
  try {
    return JSON.stringify(output, null, 2);
  } catch {
    return String(output);
  }
}

/**
 * Run a workflow-backed bot turn: the chat message becomes the trigger's Start
 * value. The run is the normal TEST envelope (no write/network/trading effects),
 * so a chat bot can never act on the world just because it was messaged.
 */
export async function runWorkflowTurn(args: {
  bot: AutomationView;
  text: string;
  signal?: AbortSignal;
  onDelta?: (chunk: string) => void;
  extra?: Record<string, unknown>;
  /** Poll interval; tests pass 0. */
  pollMs?: number;
}): Promise<string> {
  const { bot, text, signal, onDelta, extra, pollMs = 600 } = args;
  const configured = bot.action.config?.graphWorkflowId;
  const workflowId = (typeof configured === 'string' && configured) || bot.action.workflowId;
  if (!workflowId) throw new Error('This workflow bot has no workflow configured.');

  const run = await startWorkflowRun(workflowId, undefined, {
    runInput: {
      message: text,
      botId: bot.id,
      ...(extra ?? {}),
    },
  });

  const started = Date.now();
  let current = run;
  while (!isTerminal(current.status)) {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (Date.now() - started > 120_000) {
      throw new Error('The workflow did not finish within 2 minutes.');
    }
    await new Promise((resolve) => setTimeout(resolve, pollMs));
    current = await getGraphRun(run.id);
  }

  if (current.status !== 'succeeded') {
    throw new Error(current.error || `The workflow ended ${current.status}.`);
  }
  const reply = formatWorkflowOutput(current.output);
  onDelta?.(reply);
  return reply;
}

export async function runBotTurn(args: RunBotTurnArgs): Promise<string> {
  const { bot, text, prior, model, signal, onDelta, onEvent } = args;
  if (bot.action.type === 'execute_workflow') {
    return runWorkflowTurn({ bot, text, signal, onDelta, extra: { context: bot.action.workflowInput ?? {} } });
  }
  // The bot's OWN brain and model pin answer the turn, exactly like its
  // scheduled runs. The caller's model (e.g. the main-chat composer) wins when
  // present; in that case the bot's provider pin is dropped so a model is never
  // paired with the wrong provider.
  const pin = botModelPin(bot);
  // Reference images travel as real image parts; the lesson digest travels as a
  // system message so the bot can discuss failures without re-reading the panel.
  const images = referenceImages(bot);
  const learning = await tradingChatContext(bot);
  const messages = buildBotMessages(bot, text, prior);
  if (learning) messages.splice(1, 0, { role: 'system', content: learning });
  return getAgentProvider().chat(messages, undefined, {
    agentId: bot.action.agentId || 'eclipse-agent',
    chatId: botChatId(bot.id),
    workspaceRootId: botWorkspaceRootId(bot.id),
    memoryScope: 'conversation',
    model: model || pin.model,
    ...(model ? {} : { modelProvider: pin.provider }),
    ...(images.length ? { images } : {}),
    signal,
    onDelta,
    onEvent,
  });
}

export interface RunGroupMemberTurnArgs {
  group: BotGroup;
  bot: AutomationView;
  text: string;
  prior: readonly PriorGroupTurn[];
  model?: string;
  signal?: AbortSignal;
  onDelta?: (chunk: string) => void;
  /** Typed harness events for this turn (approvals etc.), when the caller needs them. */
  onEvent?: (event: HarnessUiEvent) => void;
  /** Bounded shared-room context packet (group name, request, recent replies). */
  contextPacket?: string;
}

/**
 * Run one group member's turn and resolve its final reply. Members never share a
 * reply: each streams into its own message via `onDelta`.
 */
export async function runGroupMemberTurn(args: RunGroupMemberTurnArgs): Promise<string> {
  const { group, bot, text, prior, model, signal, onDelta, onEvent, contextPacket } = args;
  if (bot.action.type === 'execute_workflow') {
    return runWorkflowTurn({ bot, text, signal, onDelta, extra: { group: group.name } });
  }
  const pin = botModelPin(bot);
  const images = referenceImages(bot);
  return getAgentProvider().chat(buildGroupMemberMessages(group, bot, text, prior, 8, contextPacket), undefined, {
    agentId: bot.action.agentId || 'eclipse-agent',
    chatId: groupMemberChatId(group.id, bot.id),
    workspaceRootId: botWorkspaceRootId(bot.id),
    memoryScope: 'conversation',
    model: model || pin.model,
    ...(model ? {} : { modelProvider: pin.provider }),
    ...(images.length ? { images } : {}),
    signal,
    onDelta,
    onEvent,
  });
}
