// ── Bot export / import ──────────────────────────────────────────────────────
// A portable, secret-free description of a bot. Export is a JSON document a
// person can keep; import always creates a DRAFT so a file cannot silently start
// an autonomous schedule. Runtime credentials and a webhook token never travel:
// the import generates a fresh webhook secret server-side.

import type {
  AutomationView,
  CreateAutomationInput,
} from '@/lib/harness/clients/automation-client';

export const BOT_TRANSFER_FORMAT = 'eclipse.bot' as const;
export const BOT_TRANSFER_VERSION = 1 as const;

export interface BotTransfer {
  format: typeof BOT_TRANSFER_FORMAT;
  version: typeof BOT_TRANSFER_VERSION;
  exportedAt: string;
  bot: {
    name: string;
    description?: string;
    trigger: Record<string, unknown>;
    action: Record<string, unknown>;
    execution?: Record<string, unknown>;
    retry?: Record<string, unknown>;
    concurrency?: Record<string, unknown>;
    limits?: Record<string, unknown>;
    permissions?: Record<string, unknown>;
    delivery?: Record<string, unknown>;
    executionAuthority?: Record<string, unknown>;
    tags?: string[];
  };
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** Build the portable document, stripping the webhook bearer token. */
export function exportBot(bot: AutomationView): BotTransfer {
  const trigger = clone(bot.trigger as unknown as Record<string, unknown>);
  delete trigger.webhookSecret;
  const action = clone(bot.action as unknown as Record<string, unknown>);
  // A credential reference is a name, not a secret, but importing it would point
  // at a vault entry the new machine may not have. Leave it out.
  delete action.credentialRef;
  return {
    format: BOT_TRANSFER_FORMAT,
    version: BOT_TRANSFER_VERSION,
    exportedAt: new Date().toISOString(),
    bot: {
      name: bot.name,
      description: bot.description ?? undefined,
      trigger,
      action,
      execution: clone(bot.execution as unknown as Record<string, unknown>),
      retry: clone(bot.retry as unknown as Record<string, unknown>),
      concurrency: clone(bot.concurrency as unknown as Record<string, unknown>),
      limits: clone(bot.limits as unknown as Record<string, unknown>),
      permissions: clone(bot.permissions as unknown as Record<string, unknown>),
      delivery: clone(bot.delivery as unknown as Record<string, unknown>),
      executionAuthority: clone(bot.executionAuthority as unknown as Record<string, unknown>),
      tags: [...(bot.tags ?? [])],
    },
  };
}

export function serializeBot(bot: AutomationView): string {
  return JSON.stringify(exportBot(bot), null, 2);
}

/** Parse + validate an imported document into a safe DRAFT create payload.
 *  Throws with a human-readable reason rather than creating a half-bot. */
export function importBotInput(raw: unknown): CreateAutomationInput {
  if (!raw || typeof raw !== 'object') throw new Error('That file is not a bot export.');
  const parsed = raw as Partial<BotTransfer> & { bot?: unknown };
  if (parsed.format !== BOT_TRANSFER_FORMAT) {
    throw new Error('That file is not an Eclipse bot export.');
  }
  if (parsed.version !== BOT_TRANSFER_VERSION) {
    throw new Error(`Unsupported bot export version ${String(parsed.version)}.`);
  }
  const bot = parsed.bot;
  if (!bot || typeof bot !== 'object') throw new Error('The export has no bot body.');
  const source = bot as Record<string, unknown>;
  const name = typeof source.name === 'string' ? source.name.trim() : '';
  if (!name) throw new Error('The exported bot has no name.');
  const trigger = source.trigger;
  const action = source.action;
  if (!trigger || typeof trigger !== 'object' || !('type' in trigger)) {
    throw new Error('The exported bot has no valid trigger.');
  }
  if (!action || typeof action !== 'object' || !('type' in action)) {
    throw new Error('The exported bot has no valid action.');
  }
  // Never import a webhook token or a credential reference.
  const cleanTrigger = clone(trigger as Record<string, unknown>);
  delete cleanTrigger.webhookSecret;
  const cleanAction = clone(action as Record<string, unknown>);
  delete cleanAction.credentialRef;

  return {
    name,
    description: typeof source.description === 'string' ? source.description : undefined,
    // Always a draft: an imported file must not start running on its own.
    state: 'draft',
    trigger: cleanTrigger as CreateAutomationInput['trigger'],
    action: cleanAction as CreateAutomationInput['action'],
    execution: source.execution as CreateAutomationInput['execution'],
    retry: source.retry as CreateAutomationInput['retry'],
    concurrency: source.concurrency as CreateAutomationInput['concurrency'],
    limits: source.limits as CreateAutomationInput['limits'],
    permissions: source.permissions as CreateAutomationInput['permissions'],
    delivery: source.delivery as CreateAutomationInput['delivery'],
    executionAuthority: source.executionAuthority as CreateAutomationInput['executionAuthority'],
    tags: Array.isArray(source.tags)
      ? source.tags.filter((tag): tag is string => typeof tag === 'string')
      : ['bot'],
  };
}

/** Browser download helper (no server round-trip). */
export function downloadBot(bot: AutomationView): void {
  const blob = new Blob([serializeBot(bot)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${bot.name.replace(/[^a-z0-9._-]+/gi, '-').slice(0, 60) || 'bot'}.bot.json`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
