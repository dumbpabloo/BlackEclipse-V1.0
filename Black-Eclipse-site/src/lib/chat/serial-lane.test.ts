// The lane exists because a parallel burst of transcript writes starved the
// app's boot fetches (global "LOADING"). These tests pin the two properties the
// fix depends on: never more than one in flight, and a failure does not block
// the queue.

import { describe, expect, it } from 'vitest';

import { createSerialLane } from './serial-lane';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('createSerialLane', () => {
  it('runs one task at a time and preserves order', async () => {
    const enqueue = createSerialLane();
    const first = deferred<string>();
    const second = deferred<string>();
    const started: string[] = [];

    const a = enqueue(async () => {
      started.push('a');
      return first.promise;
    });
    const b = enqueue(async () => {
      started.push('b');
      return second.promise;
    });

    await Promise.resolve();
    await Promise.resolve();
    expect(started).toEqual(['a']);

    first.resolve('a-done');
    expect(await a).toBe('a-done');
    // Give the lane a microtask turn to start the second task.
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toEqual(['a', 'b']);

    second.resolve('b-done');
    expect(await b).toBe('b-done');
  });

  it('releases the lane after a rejection', async () => {
    const enqueue = createSerialLane();
    const failed = enqueue(async () => {
      throw new Error('write failed');
    });
    await expect(failed).rejects.toThrow('write failed');
    await expect(enqueue(async () => 'next')).resolves.toBe('next');
  });
});
