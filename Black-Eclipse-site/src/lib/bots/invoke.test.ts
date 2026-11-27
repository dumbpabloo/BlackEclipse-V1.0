import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { AutomationView } from '@/lib/harness/clients/automation-client';
import { getGraphRun, startWorkflowRun } from '@/lib/graph-studio/execution-client';
import { runBotTurn, runWorkflowTurn, botPersona } from './invoke';

const { mockChat } = vi.hoisted(() => ({ mockChat: vi.fn() }));
const { mockGetBotTrading } = vi.hoisted(() => ({ mockGetBotTrading: vi.fn() }));

vi.mock('@/lib/harness/clients/automation-browser', () => ({
  getBotTrading: mockGetBotTrading,
}));

vi.mock('@/lib/graph-studio/execution-client', () => ({
  startWorkflowRun: vi.fn(),
  getGraphRun: vi.fn(),
  isTerminal: (status: string) => ['succeeded', 'failed', 'cancelled'].includes(status),
}));

vi.mock('@/lib/agent/registry', () => ({
  getAgentProvider: () => ({ chat: mockChat }),
}));

const mockStart = vi.mocked(startWorkflowRun);
const mockGet = vi.mocked(getGraphRun);

beforeEach(() => {
  mockChat.mockReset();
  mockChat.mockResolvedValue('ok');
  mockGetBotTrading.mockReset();
  mockGetBotTrading.mockResolvedValue({ decisions: [] });
});

function workflowBot(workflowId: string | null = 'wf_1'): AutomationView {
  return {
    id: 'bot_1',
    name: 'Workflow bot',
    description: null,
    action: {
      type: 'execute_workflow',
      objective: 'Run it',
      agentId: null,
      workflowId,
      workflowInput: {},
      toolName: null,
      toolArgs: {},
      taskTitle: null,
      skillId: null,
      skillVersion: null,
      workspaceRoot: null,
      autonomy: null,
      notificationTemplate: null,
      credentialRef: null,
      config: {},
    },
  } as AutomationView;
}

function agentBot(config: Record<string, unknown> = {}, agentId = 'agt_brain'): AutomationView {
  const bot = workflowBot();
  return {
    ...bot,
    action: {
      ...bot.action,
      type: 'run_agent',
      agentId,
      workflowId: null,
      config,
    },
  } as AutomationView;
}

describe('agent bot turn — brain and model parity', () => {  beforeEach(() => {
    vi.clearAllMocks();
    mockChat.mockResolvedValue('ok');
  });

  it('forwards the bot brain and pinned provider/model to the chat run', async () => {
    await runBotTurn({
      bot: agentBot({ modelSettings: { provider: 'openai', model: 'gpt-pinned' } }),
      text: 'hi',
      prior: [],
    });
    expect(mockChat).toHaveBeenCalledWith(
      expect.any(Array),
      undefined,
      expect.objectContaining({
        agentId: 'agt_brain',
        chatId: 'bot-bot_1',
        model: 'gpt-pinned',
        modelProvider: 'openai',
      }),
    );
  });

  it('gives a side thread its own conversation id', async () => {
    await runBotTurn({
      bot: agentBot({}),
      text: 'hi',
      prior: [],
      threadId: 'thr_side',
    });
    expect(mockChat).toHaveBeenCalledWith(
      expect.any(Array),
      undefined,
      expect.objectContaining({ chatId: 'bot-bot_1-t-thr_side' }),
    );
  });

  it('lets a caller model win without carrying the bot provider pin', async () => {
    await runBotTurn({
      bot: agentBot({ modelSettings: { provider: 'openai', model: 'gpt-pinned' } }),
      text: 'hi',
      prior: [],
      model: 'caller-model',
    });
    const options = mockChat.mock.calls[0][2] as { model?: string; modelProvider?: string };
    expect(options.model).toBe('caller-model');
    expect(options.modelProvider).toBeUndefined();
  });

  it('defaults an unpinned bot to the standard Eclipse brain', async () => {
    await runBotTurn({ bot: agentBot({}, 'eclipse-agent'), text: 'hi', prior: [] });
    const options = mockChat.mock.calls[0][2] as { agentId?: string; model?: string };
    expect(options.agentId).toBe('eclipse-agent');
    expect(options.model).toBeUndefined();
  });
});

describe('workflow bot turn', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('passes the message as run input and returns the terminal output', async () => {
    mockStart.mockResolvedValue({ id: 'run_1', status: 'running' } as never);
    mockGet
      .mockResolvedValueOnce({ id: 'run_1', status: 'running' } as never)
      .mockResolvedValueOnce({
        id: 'run_1',
        status: 'succeeded',
        output: { message: 'hello from the graph' },
      } as never);

    const deltas: string[] = [];
    const reply = await runWorkflowTurn({
      bot: workflowBot(),
      text: 'hi',
      onDelta: (chunk) => deltas.push(chunk),
      pollMs: 0,
    });

    expect(mockStart).toHaveBeenCalledWith('wf_1', undefined, {
      runInput: { message: 'hi', botId: 'bot_1' },
    });
    expect(reply).toBe('hello from the graph');
    expect(deltas).toEqual(['hello from the graph']);
  });

  it('surfaces a failed workflow as an error', async () => {
    mockStart.mockResolvedValue({ id: 'run_1', status: 'running' } as never);
    mockGet.mockResolvedValue({ id: 'run_1', status: 'failed', error: 'boom' } as never);
    await expect(runWorkflowTurn({ bot: workflowBot(), text: 'hi', pollMs: 0 }))
      .rejects.toThrow('boom');
  });

  it('refuses a bot without a workflow', async () => {
    await expect(runWorkflowTurn({ bot: workflowBot(null), text: 'hi', pollMs: 0 }))
      .rejects.toThrow(/no workflow configured/i);
    expect(mockStart).not.toHaveBeenCalled();
  });
});


describe('trading persona', () => {
  it('carries the mode, the authority and the reference images as guidance', () => {
    const bot = agentBot({
      mode: 'demo',
      visuals: [
        {
          id: 'vis_1',
          dataUrl: 'data:image/png;base64,AA==',
          caption: 'enter after the retest',
          mime: 'image/png',
          bytes: 1,
          addedAt: '2026-01-01T00:00:00Z',
        },
      ],
    });
    bot.executionAuthority = {
      tier: 'l1',
      appetite: 'conservative',
      sizingMode: 'risk_based',
      requireStop: true,
      maxOpenPositions: 1,
      maxMarginPerOrder: 500,
      cooldownS: 900,
      maxDailyLossPct: 3,
      autoMode: true,
      autoRenew: true,
      learningEnabled: true,
      improveAfterFailures: 3,
    };
    const persona = botPersona(bot);
    expect(persona).toContain('Trading mode: demo');
    expect(persona).toContain('L1');
    expect(persona).toContain('enter after the retest');
    expect(persona).toMatch(/not live market data/i);
  });

  it('adds nothing for a bot that does not trade', () => {
    const persona = botPersona(agentBot());
    expect(persona).not.toContain('Trading mode');
    expect(persona).not.toContain('reference images');
  });
});

describe('trading context on a bot turn', () => {
  const visual = {
    id: 'vis_1',
    dataUrl: 'data:image/png;base64,AA==',
    caption: 'enter after the retest',
    mime: 'image/png',
    bytes: 1,
    addedAt: '2026-01-01T00:00:00Z',
  };

  it('sends reference images as real image parts', async () => {
    const bot = agentBot({ mode: 'demo', visuals: [visual] });
    await runBotTurn({ bot, text: 'what is the setup?', prior: [] });
    const options = mockChat.mock.calls[0][2] as { images?: Array<{ dataUrl: string }> };
    expect(options.images).toHaveLength(1);
    expect(options.images?.[0].dataUrl).toContain('data:image/png');
  });

  it('adds a lesson system message from repeated failures', async () => {
    mockGetBotTrading.mockResolvedValue({
      decisions: Array.from({ length: 3 }, (_, i) => ({
        botId: 'bot_1',
        botVersion: 1,
        strategyId: 'ema_trend',
        strategyVersion: '1.0.0',
        mode: 'demo',
        tier: 'l1',
        barTs: `2026-11-17T0${i}:00:00Z`,
        symbol: 'EURUSD',
        signal: 'long_entry',
        action: 'none',
        reason: 'order refused',
        indicators: {},
        risk: null,
        outcome: 'refused',
        code: 'RISK_REFUSED',
        orderId: null,
        orderStatus: null,
        warnings: [],
      })),
    });
    const bot = agentBot({
      mode: 'demo',
      strategyId: 'ema_trend',
      symbols: ['EURUSD'],
      learning: { enabled: true, threshold: 3 },
    });
    await runBotTurn({ bot, text: 'why are you not trading?', prior: [] });
    const messages = mockChat.mock.calls[0][0] as Array<{ role: string; content: string }>;
    const system = messages.find((m) => m.role === 'system');
    expect(system?.content).toContain('RISK_REFUSED');
    expect(system?.content).toMatch(/needs their approval/i);
  });

  it('does not fetch trading status for an ordinary bot', async () => {
    await runBotTurn({ bot: agentBot(), text: 'hello', prior: [] });
    expect(mockGetBotTrading).not.toHaveBeenCalled();
  });
});
