import { describe, expect, it } from 'vitest';

import type { AutomationView } from '@/lib/harness/clients/automation-client';
import { exportBot, importBotInput, serializeBot } from './transfer';

function view(): AutomationView {
  return {
    id: 'auto_1',
    name: 'Morning brief',
    description: 'Brief me',
    state: 'active',
    version: 3,
    tags: ['bot'],
    trigger: {
      type: 'webhook',
      cron: null,
      intervalSeconds: null,
      runAt: null,
      eventType: null,
      eventFilter: {},
      condition: null,
      webhookSecret: 'super-secret',
      timezone: 'UTC',
      startAt: null,
      endAt: null,
    },
    action: {
      type: 'run_agent',
      objective: 'Brief me',
      agentId: 'eclipse-agent',
      workflowId: null,
      workflowInput: {},
      toolName: null,
      toolArgs: {},
      taskTitle: null,
      skillId: null,
      skillVersion: null,
      workspaceRoot: null,
      autonomy: null,
      notificationTemplate: null,
      credentialRef: 'vault:key',
      config: {},
    },
    execution: { misfire: 'run_once', catchUpMax: 1, maxRuntimeS: 1200, timeoutS: null },
    retry: { maxAttempts: 2, jitter: true },
    concurrency: { onConflict: 'skip_if_running', maxConcurrentRuns: 1 },
    limits: { maxRuntimeS: null, maxTokens: null, maxCost: null, maxToolCalls: null, maxRetries: null },
    permissions: { allowed: [], denied: [], requiresApproval: false, approvalScope: null },
    delivery: { onSuccess: true, onFailure: true, onApprovalRequired: true, onConditionTriggered: true, onTimeout: true, adapter: 'in_app', title: null, destination: null, targets: [] },
    executionAuthority: { tier: 'l0', modes: [], symbols: [] },
    condition: null,
    nextRunAt: null,
    lastRunAt: null,
    createdAt: '2026-11-01T00:00:00Z',
    updatedAt: '2026-11-01T00:00:00Z',
  } as unknown as AutomationView;
}

describe('bot export/import', () => {
  it('never exports the webhook token or a credential reference', () => {
    const document = exportBot(view());
    expect(document.bot.trigger.webhookSecret).toBeUndefined();
    expect(document.bot.action.credentialRef).toBeUndefined();
    expect(serializeBot(view())).not.toContain('super-secret');
  });

  it('imports as a draft and strips secrets again', () => {
    const input = importBotInput(exportBot(view()));
    expect(input.state).toBe('draft');
    expect((input.trigger as Record<string, unknown>).webhookSecret).toBeUndefined();
    expect(input.name).toBe('Morning brief');
  });

  it('refuses a foreign document', () => {
    expect(() => importBotInput({ format: 'something.else', version: 1 })).toThrow(/Eclipse bot export/);
  });
});
