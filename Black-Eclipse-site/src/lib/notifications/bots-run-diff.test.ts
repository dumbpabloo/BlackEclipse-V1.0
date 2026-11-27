import { describe, expect, it } from 'vitest';
import {
  seedBotRuns,
  selectBotRunTransitions,
  type WatchedBotRun,
} from './bots-run-diff';

function view(partial: Partial<WatchedBotRun> & { automationId: string }): WatchedBotRun {
  return {
    automationName: 'Research Bot',
    executionId: 'e1',
    state: 'running',
    ...partial,
  };
}

describe('seedBotRuns', () => {
  it('records every bot without producing notifications', () => {
    const seeded = seedBotRuns([
      view({ automationId: 'a', executionId: 'e1', state: 'running' }),
      view({ automationId: 'b', executionId: null, state: 'none' }),
    ]);
    expect(seeded.get('a')).toEqual({ executionId: 'e1', state: 'running' });
    expect(seeded.get('b')).toEqual({ executionId: null, state: 'none' });
  });
});

describe('selectBotRunTransitions', () => {
  it('notifies when a tracked run completes', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'running' })]);
    const { notifications, next } = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e1', state: 'completed', outcomeReason: 'Report delivered' })],
      prev,
    );
    expect(notifications).toHaveLength(1);
    expect(notifications[0]).toMatchObject({
      id: 'bot-run:a:e1',
      automationId: 'a',
      title: 'Bot run completed',
      body: 'Research Bot — Report delivered',
      ok: true,
    });
    expect(next.get('a')).toEqual({ executionId: 'e1', state: 'completed' });
  });

  it('uses the run output as the notification body when present', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'running' })]);
    const { notifications } = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e1', state: 'completed', output: 'The answer is 42.' })],
      prev,
    );
    expect(notifications[0]).toMatchObject({
      id: 'bot-run:a:e1',
      body: 'Research Bot — The answer is 42.',
      ok: true,
    });
  });

  it('never notifies for a quiet completed run, but still tracks it', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'running' })]);
    const { notifications, next } = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e1', state: 'completed', output: '[[no-update]]' })],
      prev,
    );
    expect(notifications).toEqual([]);
    expect(next.get('a')).toEqual({ executionId: 'e1', state: 'completed' });
  });

  it('never notifies on first sight of an automation (even if terminal)', () => {
    const { notifications } = selectBotRunTransitions(
      [view({ automationId: 'new', executionId: 'e9', state: 'completed' })],
      new Map(),
    );
    expect(notifications).toEqual([]);
  });

  it('notifies for a new execution that finished between two polls', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'completed' })]);
    const { notifications } = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e2', state: 'failed', errorClass: 'PROVIDER_ERROR' })],
      prev,
    );
    expect(notifications).toHaveLength(1);
    expect(notifications[0]).toMatchObject({ id: 'bot-run:a:e2', ok: false, body: 'Research Bot — PROVIDER_ERROR' });
  });

  it('tracks a new in-flight run without notifying until it finishes', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'completed' })]);
    const started = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e2', state: 'running' })],
      prev,
    );
    expect(started.notifications).toEqual([]);
    const finished = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e2', state: 'completed' })],
      started.next,
    );
    expect(finished.notifications[0]?.id).toBe('bot-run:a:e2');
  });

  it('stays quiet for cancelled and skipped runs', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'running' })]);
    const { notifications } = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e1', state: 'cancelled' })],
      prev,
    );
    expect(notifications).toEqual([]);
  });

  it('does not re-notify an unchanged terminal run', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'completed' })]);
    const { notifications } = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e1', state: 'completed' })],
      prev,
    );
    expect(notifications).toEqual([]);
  });

  it('drops vanished bots from the tracking map', () => {
    const prev = seedBotRuns([
      view({ automationId: 'a', executionId: 'e1' }),
      view({ automationId: 'gone', executionId: 'e2' }),
    ]);
    const { next } = selectBotRunTransitions([view({ automationId: 'a', executionId: 'e1' })], prev);
    expect(next.has('gone')).toBe(false);
  });

  it('falls back to the name when no outcome detail exists', () => {
    const prev = seedBotRuns([view({ automationId: 'a', executionId: 'e1', state: 'running' })]);
    const { notifications } = selectBotRunTransitions(
      [view({ automationId: 'a', executionId: 'e1', state: 'blocked' })],
      prev,
    );
    expect(notifications[0]).toMatchObject({ title: 'Bot run blocked', body: 'Research Bot', ok: false });
  });
});
