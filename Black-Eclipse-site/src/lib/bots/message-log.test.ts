import { describe, expect, it } from 'vitest';

import { decodePayload, mergeMessageLog } from './message-log';

interface Msg {
  id: string;
  at: string;
  content: string;
}

describe('mergeMessageLog', () => {
  it('keeps local-only messages and adds server-only messages in time order', () => {
    const local: Msg[] = [
      { id: 'b', at: '2026-11-26T10:01:00Z', content: 'local second' },
    ];
    const remote: Msg[] = [
      { id: 'a', at: '2026-11-26T10:00:00Z', content: 'server first' },
      { id: 'c', at: '2026-11-26T10:02:00Z', content: 'server third' },
    ];
    expect(mergeMessageLog(local, remote).map((m) => m.id)).toEqual(['a', 'b', 'c']);
  });

  it('lets the durable row win for the same message id', () => {
    const local: Msg[] = [{ id: 'a', at: '2026-11-26T10:00:00Z', content: 'streaming draft' }];
    const remote: Msg[] = [{ id: 'a', at: '2026-11-26T10:00:00Z', content: 'final answer' }];
    const merged = mergeMessageLog(local, remote);
    expect(merged).toHaveLength(1);
    expect(merged[0].content).toBe('final answer');
  });
});

describe('decodePayload', () => {
  it('returns the object and never throws on malformed JSON', () => {
    expect(decodePayload('{"a":1}')).toEqual({ a: 1 });
    expect(decodePayload('not json')).toBeUndefined();
    expect(decodePayload(null)).toBeUndefined();
  });
});
