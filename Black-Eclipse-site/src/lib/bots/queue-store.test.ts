import { beforeEach, describe, expect, it } from 'vitest';
import { useBotQueueStore } from './queue-store';

const take = () => useBotQueueStore.getState();

describe('durable bot turn queue', () => {
  beforeEach(() => {
    take().clear('b1');
  });

  it('queues in composition order and drains from the front', () => {
    take().enqueue({ botId: 'b1', threadId: 'main', text: 'first' });
    take().enqueue({ botId: 'b1', threadId: 'main', text: 'second' });
    expect(take().queuedByBot.b1.map((t) => t.text)).toEqual(['first', 'second']);
    expect(take().takeNext('b1')?.text).toBe('first');
    expect(take().queuedByBot.b1.map((t) => t.text)).toEqual(['second']);
  });

  it('reorders and cancels without losing other turns', () => {
    take().enqueue({ botId: 'b1', threadId: 'main', text: 'a' });
    take().enqueue({ botId: 'b1', threadId: 'main', text: 'b' });
    take().enqueue({ botId: 'b1', threadId: 'main', text: 'c' });
    const b = take().queuedByBot.b1[1];
    take().move('b1', b.id, -1);
    expect(take().queuedByBot.b1.map((t) => t.text)).toEqual(['b', 'a', 'c']);
    take().remove('b1', b.id);
    expect(take().queuedByBot.b1.map((t) => t.text)).toEqual(['a', 'c']);
  });

  it('steer promotes a queued turn to the front without duplicating it', () => {
    take().enqueue({ botId: 'b1', threadId: 'main', text: 'a' });
    take().enqueue({ botId: 'b1', threadId: 't2', text: 'steer me' });
    const target = take().queuedByBot.b1[1];
    take().promote('b1', target.id);
    expect(take().queuedByBot.b1.map((t) => t.text)).toEqual(['steer me', 'a']);
    expect(take().takeNext('b1')).toMatchObject({ text: 'steer me', threadId: 't2' });
  });

  it('keeps each bot queue separate', () => {
    take().enqueue({ botId: 'b1', threadId: 'main', text: 'one' });
    take().enqueue({ botId: 'b2', threadId: 'main', text: 'two' });
    expect(take().queuedByBot.b1).toHaveLength(1);
    expect(take().queuedByBot.b2).toHaveLength(1);
  });
});
