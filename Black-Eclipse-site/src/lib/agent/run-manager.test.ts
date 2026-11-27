import { describe, expect, it, vi, beforeEach } from 'vitest';

const chatMock = vi.fn();

vi.mock('./registry', () => ({
  ChatRequestError: class ChatRequestError extends Error {
    code?: string;
  },
  getAgentProvider: () => ({ chat: chatMock }),
  rememberActiveRun: vi.fn(),
  forgetActiveRun: vi.fn(),
  loadActiveRun: () => null,
  getRunSnapshot: vi.fn(async () => null),
  recoverRun: vi.fn(async () => ''),
}));

vi.mock('@/lib/notifications/store', () => ({
  useNotificationsStore: { getState: () => ({ activeTab: 'chat' }) },
}));
vi.mock('@/lib/notifications/focus', () => ({ isViewerFocused: () => true }));

import { runManager } from './run-manager';

function args(chatId: string) {
  return {
    chatId,
    history: [] as { role: 'user' | 'assistant'; content: string }[],
    userText: 'hello',
    model: 'test-model',
  };
}

describe('conversation-scoped run manager', () => {
  beforeEach(() => {
    chatMock.mockReset();
  });

  it('runs two conversations concurrently without either cancelling the other', async () => {
    chatMock.mockImplementation(async (
      _messages: unknown,
      _tools: unknown,
      options: { chatId?: string; onRunId: (id: string) => void },
    ) => {
      await new Promise((resolve) => setTimeout(resolve, 5));
      options.onRunId(`run_${options.chatId}`);
      return `reply:${options.chatId}`;
    });

    const [a, b] = await Promise.all([
      runManager.start(args('chat_a')),
      runManager.start(args('chat_b')),
    ]);

    expect(chatMock).toHaveBeenCalledTimes(2);
    expect(a.status).toBe('completed');
    expect(b.status).toBe('completed');
    expect(a.finalText).toBe('reply:chat_a');
    expect(b.finalText).toBe('reply:chat_b');
  });

  it('refuses a duplicate run for the SAME conversation', async () => {
    chatMock.mockImplementation(
      async () => new Promise((resolve) => setTimeout(() => resolve('ok'), 5)),
    );

    const first = runManager.start(args('chat_same'));
    const second = runManager.start(args('chat_same'));
    const [one, two] = await Promise.all([first, second]);

    expect(chatMock).toHaveBeenCalledTimes(1);
    expect(one).toBe(two);
  });

  it('forwards a resume run id to the provider so Retry continues the run', async () => {
    chatMock.mockImplementation(async () => 'continued');

    const turn = await runManager.start({ ...args('chat_resume'), resumeRunId: 'run_existing' });

    expect(turn.status).toBe('completed');
    const options = chatMock.mock.calls[0][2] as { resumeRunId?: string };
    expect(options.resumeRunId).toBe('run_existing');
  });

  it('reports both live conversations for the sidebar', async () => {
    const releases: Array<() => void> = [];
    chatMock.mockImplementation(
      async () => new Promise<string>((resolve) => {
        releases.push(() => resolve('done'));
      }),
    );

    const a = runManager.start(args('chat_live_a'));
    const b = runManager.start(args('chat_live_b'));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(runManager.getActiveKey().split('|').sort()).toEqual(['chat_live_a', 'chat_live_b']);

    expect(releases).toHaveLength(2);
    releases.forEach((release) => release());
    await Promise.all([a, b]);
    expect(runManager.getActiveKey()).toBe('');
  });

  it('shows a waiting notice until the next content frame arrives', async () => {
    let finish: (() => void) | undefined;
    chatMock.mockImplementation(async (_messages, _tools, options: {
      onEvent?: (event: { kind: string; text?: string }) => void;
    }) => {
      options.onEvent?.({ kind: 'notice', text: 'Waiting for provider capacity…' });
      await new Promise<void>((resolve) => { finish = resolve; });
      options.onEvent?.({ kind: 'delta', text: 'answer' });
      return 'answer';
    });

    const pending = runManager.start(args('chat_notice'));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(runManager.get('chat_notice').turn.statusNote).toBe('Waiting for provider capacity…');

    finish?.();
    await pending;
    // New content clears the wait note; the terminal frame clears any leftover.
    expect(runManager.get('chat_notice').turn.statusNote).toBeUndefined();
    expect(runManager.get('chat_notice').turn.status).toBe('completed');
  });
});
