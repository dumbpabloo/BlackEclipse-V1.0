import { afterEach, describe, expect, it, vi } from 'vitest';

import { subscribeCodeRun } from './code-run-stream';

class FakeEventSource {
  static last: FakeEventSource | null = null;
  readonly url: string;
  closed = false;
  onmessage: ((event: MessageEvent) => void) | null = null;
  private listeners = new Map<string, ((event: MessageEvent) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.last = this;
  }

  addEventListener(type: string, listener: EventListener) {
    const list = this.listeners.get(type) ?? [];
    list.push(listener as (event: MessageEvent) => void);
    this.listeners.set(type, list);
  }

  close() {
    this.closed = true;
  }

  emit(type: string) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ type } as MessageEvent);
    }
  }
}

describe('subscribeCodeRun', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeEventSource.last = null;
  });

  it('opens the run stream and forwards named events', () => {
    vi.stubGlobal('window', globalThis);
    vi.stubGlobal('EventSource', FakeEventSource);
    const seen: string[] = [];

    const unsubscribe = subscribeCodeRun('code_run_1', (type) => seen.push(type));

    expect(FakeEventSource.last?.url).toBe('/api/harness/runs/code_run_1/stream');
    FakeEventSource.last?.emit('step.completed');
    FakeEventSource.last?.emit('code.todo.updated');
    expect(seen).toEqual(['step.completed', 'code.todo.updated']);

    unsubscribe();
    expect(FakeEventSource.last?.closed).toBe(true);
  });

  it('does nothing without a run id', () => {
    vi.stubGlobal('window', globalThis);
    vi.stubGlobal('EventSource', FakeEventSource);
    const onEvent = vi.fn();
    const unsubscribe = subscribeCodeRun('', onEvent);
    expect(FakeEventSource.last).toBeNull();
    unsubscribe();
    expect(onEvent).not.toHaveBeenCalled();
  });
});
