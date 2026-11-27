import { describe, expect, it } from 'vitest';
import { normalizeHarnessEvent, unwrapEventPayload } from './events';
import { coerceResponsePlan, toolLabel, sourceLabel, turnText, emptyTurn } from './response-model';

describe('unwrapEventPayload', () => {
  it('unwraps a runtime envelope and carries the event type', () => {
    const out = unwrapEventPayload({
      id: 'evt_1',
      runId: 'run_1',
      type: 'model.delta',
      seq: 3,
      data: { text: 'hello' },
    });
    expect(out.text).toBe('hello');
    expect(out.eventType).toBe('model.delta');
  });

  it('passes a flat client frame through unchanged', () => {
    const out = unwrapEventPayload({ runId: 'run_1', output: 'done' });
    expect(out.output).toBe('done');
    expect(out.eventType).toBeUndefined();
  });
});

describe('normalizeHarnessEvent', () => {
  it('maps model.delta text to a delta event', () => {
    const e = normalizeHarnessEvent('model.delta', {
      type: 'model.delta',
      data: { text: 'NVDA ' },
    });
    expect(e).toEqual({ kind: 'delta', text: 'NVDA ' });
  });

  it('ignores an empty delta', () => {
    expect(normalizeHarnessEvent('model.delta', { data: { text: '' } })).toBeNull();
  });

  it('maps tool.requested to a human-labeled activity_start', () => {
    const e = normalizeHarnessEvent('tool.requested', {
      type: 'tool.requested',
      data: { name: 'market_get_quote', toolCallId: 'call_1' },
    });
    expect(e).toMatchObject({
      kind: 'activity_start',
      item: { id: 'call_1', name: 'market_get_quote', status: 'running', kind: 'tool' },
    });
    if (e?.kind === 'activity_start') {
      expect(e.item.label).toBe('Retrieving market quote');
    }
  });

  it('names Python execution explicitly instead of a vague computation label', () => {
    const e = normalizeHarnessEvent('tool.requested', {
      type: 'tool.requested',
      data: { name: 'analysis_run', toolCallId: 'call_py', detail: 'portfolio drawdown' },
    });
    expect(e).toMatchObject({
      kind: 'activity_start',
      item: { name: 'analysis_run', detail: 'portfolio drawdown', status: 'running' },
    });
    if (e?.kind === 'activity_start') expect(e.item.label).toBe('Executing Python');
    expect(toolLabel('execute_python')).toBe('Executing Python');
  });

  it('correlates tool.completed by toolCallId', () => {
    const e = normalizeHarnessEvent('tool.completed', {
      type: 'tool.completed',
      data: { name: 'market_get_quote', toolCallId: 'call_1', durationMs: 120 },
    });
    expect(e).toEqual({
      kind: 'activity_end',
      id: 'call_1',
      name: 'market_get_quote',
      status: 'completed',
      durationMs: 120,
      code: undefined,
    });
  });

  it('carries the tool detail (command/path) on tool.requested', () => {
    const e = normalizeHarnessEvent('tool.requested', {
      type: 'tool.requested',
      data: { name: 'terminal_run', toolCallId: 'call_c', detail: 'python app.py' },
    });
    expect(e).toMatchObject({
      kind: 'activity_start',
      item: { id: 'call_c', name: 'terminal_run', detail: 'python app.py' },
    });
  });

  it('carries the bounded Python code preview on tool.requested', () => {
    const e = normalizeHarnessEvent('tool.requested', {
      type: 'tool.requested',
      data: {
        name: 'analysis_run',
        toolCallId: 'call_py',
        codePreview: {
          language: 'python',
          text: 'result = 1',
          truncated: false,
          totalChars: 10,
        },
      },
    });
    expect(e).toMatchObject({
      kind: 'activity_start',
      item: {
        id: 'call_py',
        codePreview: {
          language: 'python',
          text: 'result = 1',
          truncated: false,
          totalChars: 10,
        },
      },
    });
  });

  it('drops a malformed code preview instead of half-rendering it', () => {
    const e = normalizeHarnessEvent('tool.requested', {
      type: 'tool.requested',
      data: { name: 'analysis_run', toolCallId: 'call_py', codePreview: { text: '' } },
    });
    expect(e).toMatchObject({ kind: 'activity_start', item: { id: 'call_py' } });
    if (e?.kind === 'activity_start') expect(e.item.codePreview).toBeUndefined();
  });

  it('carries the result summary on tool.completed', () => {
    const e = normalizeHarnessEvent('tool.completed', {
      type: 'tool.completed',
      data: { name: 'terminal_run', toolCallId: 'call_c', durationMs: 40, summary: 'exit 0' },
    });
    expect(e).toMatchObject({
      kind: 'activity_end',
      id: 'call_c',
      name: 'terminal_run',
      status: 'completed',
      summary: 'exit 0',
    });
  });

  it('maps code.patch.applied to a change with the runtime diff', () => {
    const e = normalizeHarnessEvent('code.patch.applied', {
      type: 'code.patch.applied',
      data: { op: 'replace', path: 'src/app.py', additions: 1, deletions: 1, diff: '-beta\n+BETA' },
    });
    expect(e).toMatchObject({
      kind: 'changes',
      items: [{ path: 'src/app.py', change: 'modified', additions: 1, deletions: 1, diff: '-beta\n+BETA' }],
    });
  });

  it('maps fs previews â€” a multi-file change arrives as several items', () => {
    const e = normalizeHarnessEvent('fs.apply_diff', {
      type: 'fs.apply_diff',
      data: {
        previews: [
          { path: 'a.ts', op: 'modify', diff: '-a\n+A', additions: 1, deletions: 1 },
          { path: 'b.ts', op: 'create', diff: '+B', additions: 1 },
        ],
      },
    });
    if (e?.kind !== 'changes') throw new Error('expected a changes event');
    expect(e.items).toHaveLength(2);
    expect(e.items[1]).toMatchObject({ path: 'b.ts', change: 'added', diff: '+B' });
  });

  it('maps a move preview to a renamed change without a diff', () => {
    const e = normalizeHarnessEvent('fs.move_file', {
      type: 'fs.move_file',
      data: { previews: [{ path: 'src/b.ts', op: 'move', from: 'src/a.ts' }] },
    });
    expect(e).toMatchObject({
      kind: 'changes',
      items: [{ path: 'src/b.ts', change: 'renamed', from: 'src/a.ts' }],
    });
  });

  it('ignores an fs event with no previews', () => {
    expect(
      normalizeHarnessEvent('fs.transaction.committed', {
        type: 'fs.transaction.committed',
        data: { filesChanged: 2 },
      }),
    ).toBeNull();
  });

  it('surfaces tool.failed as a failed activity with its code', () => {
    const e = normalizeHarnessEvent('tool.failed', {
      type: 'tool.failed',
      data: { name: 'research_search', toolCallId: 'call_2', code: 'TOOL_ERROR' },
    });
    expect(e).toMatchObject({ kind: 'activity_end', status: 'failed', code: 'TOOL_ERROR' });
  });

  it('surfaces a refused code step as a plain reasonKey, not the raw code', () => {
    const e = normalizeHarnessEvent('code.refused', {
      type: 'code.refused',
      data: { tool: 'workspace_patch_apply', code: 'PROFILE_DENIED' },
    });
    expect(e).toMatchObject({
      kind: 'activity_start',
      item: { name: 'workspace_patch_apply', status: 'completed', code: 'PROFILE_DENIED' },
    });
    if (e?.kind === 'activity_start') {
      expect(e.item.reasonKey).toBe('refusal.PROFILE_DENIED');
      // The raw code never becomes the label shown to a user.
      expect(e.item.label).not.toContain('PROFILE_DENIED');
    }
  });

  it('a patch refusal maps to its refusal key too', () => {
    const e = normalizeHarnessEvent('code.patch.refused', {
      type: 'code.patch.refused',
      data: { op: 'replace', path: 'src/x.ts', code: 'PATCH_PROTECTED_PATH' },
    });
    expect(e).toMatchObject({ kind: 'activity_start', item: { code: 'PATCH_PROTECTED_PATH' } });
    if (e?.kind === 'activity_start') {
      expect(e.item.reasonKey).toBe('refusal.PATCH_PROTECTED_PATH');
    }
  });

  it('maps retrieval source events, falling back to a composite id', () => {
    const start = normalizeHarnessEvent('retrieval.source_start', {
      type: 'retrieval.source_start',
      data: { source: 'yfinance', dataType: 'quote' },
    });
    expect(start).toMatchObject({
      kind: 'activity_start',
      item: { id: 'retrieval:yfinance:quote', kind: 'retrieval', name: 'yfinance' },
    });
    if (start?.kind === 'activity_start') {
      expect(start.item.label).toBe('Consulting Yahoo Finance');
    }

    const ok = normalizeHarnessEvent('retrieval.source_ok', {
      type: 'retrieval.source_ok',
      data: { source: 'yfinance', dataType: 'quote' },
    });
    expect(ok).toMatchObject({
      kind: 'activity_end',
      id: 'retrieval:yfinance:quote',
      status: 'completed',
    });
  });

  it('reports a retrieval record count only when the runtime sends one', () => {
    const withCount = normalizeHarnessEvent('retrieval.source_ok', {
      type: 'retrieval.source_ok',
      data: { source: 'jdds', dataType: 'quote', objects: 3 },
    });
    expect(withCount).toMatchObject({ kind: 'activity_end', summary: '3 records' });

    const one = normalizeHarnessEvent('retrieval.source_ok', {
      type: 'retrieval.source_ok',
      data: { source: 'jdds', dataType: 'quote', objects: 1 },
    });
    expect(one).toMatchObject({ kind: 'activity_end', summary: '1 record' });

    const unknown = normalizeHarnessEvent('retrieval.source_ok', {
      type: 'retrieval.source_ok',
      data: { source: 'jdds', dataType: 'quote' },
    });
    expect((unknown as { summary?: string }).summary).toBeUndefined();
  });

  it('maps client.final to the authoritative output', () => {
    const e = normalizeHarnessEvent('client.final', { runId: 'r', status: 'completed', output: 'Answer' });
    expect(e).toEqual({
      kind: 'final',
      output: 'Answer',
      error: undefined,
      status: 'completed',
      research: undefined,
      memory: undefined,
      runId: 'r',
    });
  });

  it('carries a typed command UI intent on client.final', () => {
    const ui = { type: 'navigate' as const, tab: 'alerts' as const };
    const e = normalizeHarnessEvent('client.final', {
      runId: 'r',
      status: 'completed',
      output: 'Opening alerts.',
      ui,
    });
    expect(e).toMatchObject({ kind: 'final', ui });
  });

  it('carries Phase 4 research metadata on client.final', () => {
    const research = { status: 'partial', metadata: { limitations: ['x'] } };
    const e = normalizeHarnessEvent('client.final', {
      runId: 'r',
      status: 'completed',
      output: 'Answer',
      research,
    });
    expect(e).toMatchObject({ kind: 'final', research });
  });

  it('maps run.failed to an error with code', () => {
    const e = normalizeHarnessEvent('run.failed', {
      type: 'run.failed',
      data: { error: { code: 'PROVIDER_ERROR', message: 'boom' } },
    });
    expect(e).toEqual({ kind: 'error', message: 'boom', code: 'PROVIDER_ERROR' });
  });

  it('maps lifecycle events to coarse status', () => {
    expect(normalizeHarnessEvent('run.started', { type: 'run.started', data: {} })).toEqual({
      kind: 'status',
      status: 'planning',
    });
    expect(normalizeHarnessEvent('run.completed', { type: 'run.completed', data: {} })).toEqual({
      kind: 'status',
      status: 'completed',
    });
    expect(normalizeHarnessEvent('run.cancelled', { type: 'run.cancelled', data: {} })).toEqual({
      kind: 'status',
      status: 'cancelled',
    });
  });

  it('surfaces a history trim as one bounded activity line (counts only)', () => {
    const e = normalizeHarnessEvent('memory.history_bounded', {
      type: 'memory.history_bounded',
      data: {
        keptTurns: 20,
        droppedTurns: 10,
        droppedTokensEstimate: 5000,
        window: 32000,
        windowKnown: true,
        tighterRetry: false,
      },
    });
    expect(e).toMatchObject({
      kind: 'activity_start',
      item: { id: 'history_bounded', kind: 'bounded', status: 'completed' },
    });
    if (e?.kind === 'activity_start') {
      expect(e.item.label).toContain('Trimmed 10 older turns');
      expect(e.item.summary).toContain('20 kept');
      expect(e.item.summary).toContain('32,000-token window');
    }
  });

  it('reports an unknown window honestly on the bounded line', () => {
    const e = normalizeHarnessEvent('memory.history_bounded', {
      type: 'memory.history_bounded',
      data: { keptTurns: 20, droppedTurns: 10, window: null, windowKnown: false },
    });
    if (e?.kind === 'activity_start') {
      expect(e.item.summary).toContain('context window unknown');
    } else {
      throw new Error('expected an activity_start');
    }
  });

  it('emits nothing for a history_bounded with no dropped turns', () => {
    expect(
      normalizeHarnessEvent('memory.history_bounded', {
        type: 'memory.history_bounded',
        data: { keptTurns: 5, droppedTurns: 0, window: 32000, windowKnown: true },
      }),
    ).toBeNull();
  });

  it('surfaces memory recall as one recall activity line (counts only)', () => {
    const e = normalizeHarnessEvent('memory.retrieved', {
      type: 'memory.retrieved',
      data: {
        selected: 3,
        candidates: 12,
        usedChars: 2400,
        budgetChars: 4000,
        // The full payload the runtime sends â€” none of this may reach the line.
        selectedIds: ['mem_secret_1', 'mem_secret_2'],
        excluded: [{ id: 'mem_no', reason: 'scope mismatch' }],
        selectionReasons: [{ id: 'mem_secret_1', reason: 'topic match' }],
      },
    });
    expect(e).toMatchObject({
      kind: 'activity_start',
      item: { id: 'memory_recalled', kind: 'recall', status: 'completed' },
    });
    if (e?.kind === 'activity_start') {
      expect(e.item.label).toBe('Recalled 3 memories from long-term memory');
      expect(e.item.summary).toBe('2,400 chars recalled');
      // Never memory content, ids or excluded text.
      const surface = `${e.item.label} ${e.item.summary ?? ''}`;
      expect(surface).not.toContain('mem_secret');
      expect(surface).not.toContain('scope mismatch');
    }
  });

  it('uses the singular form for a single recalled memory', () => {
    const e = normalizeHarnessEvent('memory.retrieved', {
      type: 'memory.retrieved',
      data: { selected: 1, usedChars: 90 },
    });
    if (e?.kind === 'activity_start') {
      expect(e.item.label).toBe('Recalled 1 memory from long-term memory');
    } else {
      throw new Error('expected an activity_start');
    }
  });

  it('emits nothing when no memory was recalled', () => {
    expect(
      normalizeHarnessEvent('memory.retrieved', {
        type: 'memory.retrieved',
        data: { selected: 0, usedChars: 0 },
      }),
    ).toBeNull();
    expect(normalizeHarnessEvent('memory.retrieved', { type: 'memory.retrieved', data: {} })).toBeNull();
  });

  it('returns null (never throws) for a malformed memory.retrieved payload', () => {
    expect(
      normalizeHarnessEvent('memory.retrieved', {
        type: 'memory.retrieved',
        data: { selected: 'lots', usedChars: null },
      }),
    ).toBeNull();
  });

  it('maps a CONTEXT_OVERFLOW run.failed with its specific message', () => {
    const e = normalizeHarnessEvent('run.failed', {
      type: 'run.failed',
      data: {
        error: {
          code: 'CONTEXT_OVERFLOW',
          message: 'The request exceeded the model\'s context window. Start a shorter conversation.',
        },
      },
    });
    expect(e).toMatchObject({ kind: 'error', code: 'CONTEXT_OVERFLOW' });
    if (e?.kind === 'error') expect(e.message).toContain('shorter conversation');
  });
});

describe('response-model helpers', () => {
  it('prefers finalText over streamed text', () => {
    expect(turnText({ ...emptyTurn(), text: 'partial', finalText: 'final' })).toBe('final');
    expect(turnText({ ...emptyTurn(), text: 'partial' })).toBe('partial');
  });

  it('prettifies unknown tool names instead of leaking snake_case', () => {
    expect(toolLabel('some_unknown_thing')).toBe('Using some unknown thing');
  });

  it('labels known sources and passes unknown ones through', () => {
    expect(sourceLabel('jdds')).toBe('market data');
    expect(sourceLabel('custom-vendor')).toBe('custom-vendor');
  });
});

describe('analysis.completed (R3-06 computed results)', () => {
  it('maps a completed computation to an analysis event with the result blob', () => {
    const result = {
      status: 'ok',
      outputs: { sharpe: 1.42 },
      artifacts: [{ name: 's.csv', sha256: 'abc123', bytes: 10, mediaType: 'text/csv' }],
      provenance: { kind: 'computed', label: 'Worked out by Eclipse', detail: 'Sharpe' },
    };
    const e = normalizeHarnessEvent('analysis.completed', {
      type: 'analysis.completed',
      data: { status: 'ok', result },
    });
    expect(e).toEqual({ kind: 'analysis', result });
  });

  it('ignores a completion without a result blob', () => {
    expect(
      normalizeHarnessEvent('analysis.completed', { type: 'analysis.completed', data: { status: 'ok' } }),
    ).toBeNull();
  });
});

describe('acquire.completed (Phase 16 collection manifest)', () => {
  it('maps a completed collection to an acquisition event with the manifest', () => {
    const manifest = {
      purpose: 'watchlist',
      sources: [{ id: 'prices_daily', label: 'Daily price history', rows: 4, complete: true }],
    };
    const e = normalizeHarnessEvent('acquire.completed', {
      type: 'acquire.completed',
      data: { status: 'ok', manifest },
    });
    expect(e).toEqual({ kind: 'acquisition', manifest });
  });

  it('ignores a completion without a manifest object', () => {
    expect(
      normalizeHarnessEvent('acquire.completed', { type: 'acquire.completed', data: { status: 'ok' } }),
    ).toBeNull();
    expect(
      normalizeHarnessEvent('acquire.completed', {
        type: 'acquire.completed',
        data: { manifest: ['not', 'a', 'manifest'] },
      }),
    ).toBeNull();
  });
});

describe('response.plan (Intelligent Response Orchestrator)', () => {
  const plan = {
    mode: 'visualization',
    confidence: 'certain',
    reason: 'The turn matches the visualization signals.',
    signals: ['mode:visualization'],
    suggestedBlocks: ['chart', 'data_table'],
    learning: false,
  };

  it('maps a response.plan envelope to a response_plan event', () => {
    const e = normalizeHarnessEvent('response.plan', {
      type: 'response.plan',
      data: plan,
    });
    expect(e).toEqual({ kind: 'response_plan', plan });
  });

  it('drops a response.plan with an unknown mode', () => {
    expect(
      normalizeHarnessEvent('response.plan', { type: 'response.plan', data: { mode: 'hologram' } }),
    ).toBeNull();
  });

  it('coerces a partial payload to safe defaults', () => {
    const coerced = coerceResponsePlan({ mode: 'text' });
    expect(coerced).toEqual({
      mode: 'text',
      confidence: 'unsure',
      reason: '',
      signals: [],
      suggestedBlocks: [],
      learning: false,
    });
  });

  it('returns undefined for a non-object plan', () => {
    expect(coerceResponsePlan('nope')).toBeUndefined();
    expect(coerceResponsePlan(null)).toBeUndefined();
    expect(coerceResponsePlan(['visualization'])).toBeUndefined();
  });
});

describe('capability lifecycle events', () => {
  it('maps a capability request to the typed capability event', () => {
    const e = normalizeHarnessEvent('capability.requested', {
      type: 'capability.requested',
      data: {
        requestId: 'capreq_1', capabilityId: 'ulcer_index', need: 'compute the ulcer index',
        kind: 'recipe', status: 'gap', surface: 'capability_builder', scope: 'workspace',
        requiresApproval: false, steps: ['write a manifest'], missing: ['contract test'],
      },
    });
    expect(e).toMatchObject({
      kind: 'capability',
      phase: 'requested',
      view: { capabilityId: 'ulcer_index', status: 'gap' },
    });
  });

  it('maps a capability offer to the same typed card', () => {
    const e = normalizeHarnessEvent('capability.offer', {
      type: 'capability.offer',
      data: {
        gapId: 'gap_1', need: 'turn this into a bot', kind: 'context_denied',
        code: 'PROFILE_DENIED', status: 'handoff_required', surface: 'bot_create',
        nextTool: 'bot_create', actionLabel: 'Create the bot',
        actionPrompt: 'Create the bot for: turn this into a bot', offer: true,
      },
    });
    expect(e).toMatchObject({
      kind: 'capability',
      phase: 'requested',
      view: { gapId: 'gap_1', nextTool: 'bot_create', offer: true },
    });
  });

  it('maps build, activation, failure, disable and retire phases', () => {
    const cases = [
      ['capability.built', 'built'],
      ['capability.activated', 'activated'],
      ['capability.verification_failed', 'verification_failed'],
      ['capability.disabled', 'disabled'],
      ['capability.retired', 'retired'],
    ] as const;
    for (const [event, phase] of cases) {
      const e = normalizeHarnessEvent(event, {
        type: event,
        data: { capabilityId: 'ulcer_index', version: '1.0.0', status: 'active' },
      });
      expect(e).toMatchObject({ kind: 'capability', phase });
    }
  });

  it('drops a malformed capability payload instead of rendering a half card', () => {
    expect(normalizeHarnessEvent('capability.built', {
      type: 'capability.built', data: {},
    })).toBeNull();
  });
});

describe('clarification.requested (human-in-the-loop question)', () => {
  it('maps a question with options to a blocking clarification card', () => {
    const e = normalizeHarnessEvent('clarification.requested', {
      type: 'clarification.requested',
      data: {
        clarificationId: 'clar_1',
        question: 'Which horizon?',
        context: 'The result differs.',
        options: ['1 year', '5 years'],
        recommended: '5 years',
      },
    });
    expect(e).toEqual({
      kind: 'clarification_request',
      clarification: {
        id: 'clar_1',
        question: 'Which horizon?',
        context: 'The result differs.',
        options: ['1 year', '5 years'],
        recommended: '5 years',
        allowCustom: true,
      },
    });
  });

  it('honours allowCustom=false and drops non-string options', () => {
    const e = normalizeHarnessEvent('clarification.requested', {
      type: 'clarification.requested',
      data: { clarificationId: 'c', question: 'Q', options: ['A', 7, null], allowCustom: false },
    });
    expect(e).toMatchObject({
      kind: 'clarification_request',
      clarification: { options: ['A'], allowCustom: false },
    });
  });

  it('drops a malformed question instead of rendering an empty card', () => {
    expect(normalizeHarnessEvent('clarification.requested', {
      type: 'clarification.requested', data: { clarificationId: 'c' },
    })).toBeNull();
  });

  it('maps clarification.answered to a clear event', () => {
    expect(normalizeHarnessEvent('clarification.answered', {
      type: 'clarification.answered', data: { clarificationId: 'clar_1' },
    })).toEqual({ kind: 'clarification_answered', id: 'clar_1' });
  });

  // ── parallel fan-out ───────────────────────────────────────────────────────

  it('maps delegation.started to rows with the user sub-questions', () => {
    const e = normalizeHarnessEvent('delegation.started', {
      type: 'delegation.started',
      data: {
        delegationId: 'dlg_1',
        children: 2,
        aggregate: 'compare',
        title: 'Review two notes',
        agents: [
          { childIndex: 1, goal: 'Review MSFT', agentId: 'eclipse-agent' },
          { childIndex: 0, goal: 'Review AAPL' },
        ],
      },
    });
    expect(e).toEqual({
      kind: 'delegation_start',
      delegation: {
        delegationId: 'dlg_1',
        aggregate: 'compare',
        strategy: 'split',
        title: 'Review two notes',
        done: false,
        children: [
          { childIndex: 0, goal: 'Review AAPL', status: 'running' },
          { childIndex: 1, goal: 'Review MSFT', status: 'running', agentId: 'eclipse-agent' },
        ],
      },
    });
  });

  it('keeps a real cost, and an explicitly unknown one, distinct', () => {
    const priced = normalizeHarnessEvent('delegation.progress', {
      type: 'delegation.progress',
      data: {
        delegationId: 'dlg_1', childIndex: 0, goal: 'g', status: 'running',
        step: 3, maxSteps: 10, tokens: 900, costUsd: 0.12,
        recent: ['Starting', 'Reading the filing'],
      },
    });
    expect(priced).toMatchObject({
      kind: 'delegation_progress',
      child: { costUsd: 0.12, step: 3, maxSteps: 10, recent: ['Starting', 'Reading the filing'] },
    });

    const unpriced = normalizeHarnessEvent('delegation.progress', {
      type: 'delegation.progress',
      data: { delegationId: 'dlg_1', childIndex: 1, status: 'running', costUsd: null },
    });
    expect(unpriced).toMatchObject({ kind: 'delegation_progress' });
    if (unpriced?.kind === 'delegation_progress') expect(unpriced.child.costUsd).toBeNull();
  });

  it('maps a child terminal state and the aggregated totals', () => {
    expect(normalizeHarnessEvent('delegation.child.failed', {
      type: 'delegation.child.failed',
      data: {
        delegationId: 'dlg_1', childIndex: 2, status: 'timed_out',
        tokens: 40, reason: 'This part took too long and was stopped.',
      },
    })).toEqual({
      kind: 'delegation_child', delegationId: 'dlg_1', childIndex: 2,
      status: 'timed_out', tokens: 40, reason: 'This part took too long and was stopped.',
    });

    expect(normalizeHarnessEvent('delegation.aggregated', {
      type: 'delegation.aggregated',
      data: { delegationId: 'dlg_1', droppedClaims: 2, tokens: 5000, completed: 2 },
    })).toEqual({ kind: 'delegation_done', delegationId: 'dlg_1', droppedClaims: 2, tokens: 5000 });
  });

  it('carries the best-of strategy and its selected part', () => {
    const startEvent = normalizeHarnessEvent('delegation.started', {
      type: 'delegation.started',
      data: {
        delegationId: 'dlg_best',
        children: 3,
        strategy: 'best_of',
        agents: [{ childIndex: 0, goal: 'Fix checkout', approach: 'mock the API' }],
      },
    });
    expect(startEvent).toMatchObject({
      kind: 'delegation_start',
      delegation: { strategy: 'best_of' },
    });
    if (startEvent?.kind === 'delegation_start') {
      expect(startEvent.delegation.children[0].approach).toBe('mock the API');
    }

    const done = normalizeHarnessEvent('delegation.aggregated', {
      type: 'delegation.aggregated',
      data: { delegationId: 'dlg_best', strategy: 'best_of', winner: 0 },
    });
    expect(done).toEqual({ kind: 'delegation_done', delegationId: 'dlg_best', winner: 0 });
  });
});

describe('worker notices (waiting / reconnecting)', () => {
  it('names a provider admission wait instead of a generic spinner', () => {
    const queued = normalizeHarnessEvent('model.queued', {
      type: 'model.queued',
      data: { reason: 'concurrency', provider: 'opencode-go' },
    });
    expect(queued).toEqual({ kind: 'notice', text: 'Waiting for provider capacity…' });

    const cooldown = normalizeHarnessEvent('model.queued', {
      type: 'model.queued',
      data: { reason: 'credential_cooldown' },
    });
    expect(cooldown?.kind).toBe('notice');
  });

  it('surfaces a stalled provider stream as a notice', () => {
    const stalled = normalizeHarnessEvent('model.stalled', {
      type: 'model.stalled',
      data: { idleTimeoutS: 90 },
    });
    expect(stalled?.kind).toBe('notice');
  });
});
