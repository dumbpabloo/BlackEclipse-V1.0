import { beforeEach, describe, expect, it } from 'vitest';
import { useBotChatStore, type BotMessage } from './chat-store';

const scheduled = (runId: string, content = 'answer'): BotMessage & { runId: string } => ({
  id: `run-${runId}`,
  role: 'assistant',
  content,
  at: '2026-01-01T00:00:00Z',
  scheduled: true,
  runId,
});

describe('bot chat store — side threads and branches', () => {
  beforeEach(() => {
    useBotChatStore.setState({
      byBot: {}, threadsByBot: {}, messagesByThread: {}, activeThreadByBot: {},
    });
  });

  it('keeps a side thread separate from the main conversation', () => {
    const state = useBotChatStore.getState();
    state.append('t1', { id: 'm1', role: 'user', content: 'main question', at: '2026-01-01T00:00:00Z' });
    const threadId = useBotChatStore.getState().createThread('t1', 'Experiment');
    useBotChatStore.getState().append('t1', {
      id: 's1', role: 'user', content: 'side question', at: '2026-01-01T00:01:00Z',
    });
    expect(useBotChatStore.getState().byBot.t1.map((m) => m.content)).toEqual(['side question']);
    useBotChatStore.getState().selectThread('t1', 'main');
    expect(useBotChatStore.getState().byBot.t1.map((m) => m.content)).toEqual(['main question']);
    expect(useBotChatStore.getState().messagesByThread[`t1::${threadId}`]).toHaveLength(1);
  });

  it('branches through the fork point with new ids and its own conversation', () => {
    const state = useBotChatStore.getState();
    state.appendTo('t1', 'main', { id: 'a', role: 'user', content: 'one', at: '2026-01-01T00:00:00Z' });
    state.appendTo('t1', 'main', { id: 'b', role: 'assistant', content: 'two', at: '2026-01-01T00:01:00Z' });
    state.appendTo('t1', 'main', { id: 'c', role: 'user', content: 'three', at: '2026-01-01T00:02:00Z' });
    const branchId = useBotChatStore.getState().branchThread('t1', 'b', 'Alternate');
    expect(branchId).toBeTruthy();
    const branch = useBotChatStore.getState().messagesByThread[`t1::${branchId}`];
    expect(branch.map((m) => m.content)).toEqual(['one', 'two']);
    // New ids: editing the branch can never mutate the main thread's rows.
    expect(branch.every((m) => m.id.startsWith('br_'))).toBe(true);
    expect(useBotChatStore.getState().activeThreadByBot.t1).toBe(branchId);
    // The branch records its origin.
    const thread = useBotChatStore.getState().threadsByBot.t1.find((t) => t.id === branchId);
    expect(thread).toMatchObject({ parentThreadId: 'main', branchFromMessageId: 'b' });
  });

  it('deleting a side thread returns to main and never deletes main', () => {
    const threadId = useBotChatStore.getState().createThread('t1');
    expect(useBotChatStore.getState().deleteThread('t1', 'main'), 'main is protected').toBeUndefined();
    useBotChatStore.getState().deleteThread('t1', threadId);
    expect(useBotChatStore.getState().activeThreadByBot.t1).toBe('main');
    expect(useBotChatStore.getState().threadsByBot.t1).toHaveLength(1);
  });

  it('records a reaction on the message it belongs to', () => {
    useBotChatStore.getState().append('t1', {
      id: 'r1', role: 'assistant', content: 'answer', at: '2026-01-01T00:00:00Z',
    });
    useBotChatStore.getState().react('t1', 'r1', '🎯');
    useBotChatStore.getState().react('t1', 'r1', '🎯');
    expect(useBotChatStore.getState().byBot.t1[0].reactions).toEqual({ '🎯': 2 });
  });
});

describe('bot chat store — autonomous run append', () => {
  beforeEach(() => {
    useBotChatStore.getState().clear('b1');
  });

  it('appends a scheduled run exactly once, keyed by execution id', () => {
    const store = useBotChatStore.getState();
    expect(store.appendScheduled('b1', scheduled('e1'))).toBe(true);
    // A re-poll (or a reload that re-syncs history) must not double-post it.
    expect(useBotChatStore.getState().appendScheduled('b1', scheduled('e1'))).toBe(false);
    expect(useBotChatStore.getState().byBot['b1']).toHaveLength(1);
  });

  it('appends distinct runs in order', () => {
    const store = useBotChatStore.getState();
    store.appendScheduled('b1', scheduled('e1', 'first'));
    useBotChatStore.getState().appendScheduled('b1', scheduled('e2', 'second'));
    expect(useBotChatStore.getState().byBot['b1'].map((m) => m.content)).toEqual([
      'first',
      'second',
    ]);
  });
});
