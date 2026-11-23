import { describe, expect, it } from 'vitest';
import { createWorkflowIR } from './ir';
import { portsCompatible, validateWorkflow } from './validation';
import { buildTemplate, WORKFLOW_TEMPLATES } from './templates';
import { getNodeDefinition } from './registry';
import type { PortDef, WorkflowIR } from './types';

function port(partial: Partial<PortDef> & Pick<PortDef, 'type'>): PortDef {
  return { id: partial.id ?? 'p', name: partial.name ?? 'P', kind: partial.kind ?? 'data', ...partial };
}

function node(ir: WorkflowIR, type: string, id: string) {
  const def = getNodeDefinition(type)!;
  return { id, type, version: def.version, config: def.defaultConfig(), layout: { x: 0, y: 0 } };
}

describe('port compatibility', () => {
  it('allows any ↔ anything', () => {
    expect(portsCompatible(port({ type: 'any' }), port({ type: 'number' }))).toBe(true);
    expect(portsCompatible(port({ type: 'string' }), port({ type: 'any' }))).toBe(true);
  });
  it('requires exact data type match', () => {
    expect(portsCompatible(port({ type: 'number' }), port({ type: 'string' }))).toBe(false);
    expect(portsCompatible(port({ type: 'ohlcv' }), port({ type: 'ohlcv' }))).toBe(true);
  });
  it('lets a ticket take an MT5 number or a REST opaque string', () => {
    expect(portsCompatible(port({ type: 'number' }), port({ type: 'ticket' }))).toBe(true);
    expect(portsCompatible(port({ type: 'string' }), port({ type: 'ticket' }))).toBe(true);
    expect(portsCompatible(port({ type: 'ticket' }), port({ type: 'ticket' }))).toBe(true);
    expect(portsCompatible(port({ type: 'boolean' }), port({ type: 'ticket' }))).toBe(false);
  });
  it('keeps control ports separate from data ports', () => {
    expect(portsCompatible(port({ type: 'any', kind: 'control' }), port({ type: 'any' }))).toBe(false);
    expect(portsCompatible(port({ type: 'any', kind: 'control' }), port({ type: 'any', kind: 'control' }))).toBe(true);
  });
});

describe('workflow validation', () => {
  it('accepts every starter template', () => {
    for (const template of WORKFLOW_TEMPLATES) {
      const ir = buildTemplate(template.id)!;
      const errors = validateWorkflow(ir).filter((d) => d.severity === 'error');
      expect(errors, template.id).toEqual([]);
    }
  });

  it('ships the six data nodes wired in the data-pipeline template', () => {
    const ir = buildTemplate('data-pipeline')!;
    const types = new Set(ir.nodes.map((n) => n.type));
    expect(types).toEqual(
      new Set([
        'trigger.manual',
        'data.value',
        'data.json.parse',
        'data.set_variables',
        'data.dataset.save',
        'data.dataset.load',
        'file.write',
        'output.result',
      ]),
    );
  });

  it('reports a missing trigger and terminal on an empty graph', () => {
    const ir = createWorkflowIR({ nodes: [node(createWorkflowIR(), 'transform.template', 't')] });
    const codes = validateWorkflow(ir).map((d) => d.code);
    expect(codes).toContain('missing_trigger');
    expect(codes).toContain('missing_terminal');
  });

  it('reports incompatible port connections', () => {
    const base = createWorkflowIR();
    const ir: WorkflowIR = {
      ...base,
      nodes: [node(base, 'trigger.manual', 'trig'), node(base, 'finance.indicator', 'ind')],
      edges: [{ id: 'e1', source: 'trig', sourceHandle: 'start', target: 'ind', targetHandle: 'series' }],
    };
    // trigger.start is `any` → allowed; force an incompatible edge explicitly:
    const bad: WorkflowIR = {
      ...ir,
      edges: [{ id: 'e1', source: 'ind', sourceHandle: 'values', target: 'trig', targetHandle: 'nope' }],
    };
    expect(validateWorkflow(bad).map((d) => d.code)).toContain('invalid_edge_handles');
  });

  it('reports a missing required input', () => {
    const base = createWorkflowIR();
    const ir: WorkflowIR = {
      ...base,
      nodes: [node(base, 'trigger.manual', 'trig'), node(base, 'data.json.parse', 'parse'), node(base, 'output.result', 'out')],
      edges: [{ id: 'e1', source: 'parse', sourceHandle: 'value', target: 'out', targetHandle: 'value' }],
    };
    const codes = validateWorkflow(ir).map((d) => d.code);
    expect(codes).toContain('missing_required_input');
  });

  it('accepts a required input supplied by a config fallback', () => {
    const base = createWorkflowIR();
    const parse = node(base, 'data.json.parse', 'parse');
    const ir: WorkflowIR = {
      ...base,
      nodes: [
        node(base, 'trigger.manual', 'trig'),
        { ...parse, config: { ...parse.config, text: '{"a":1}' } },
        node(base, 'output.result', 'out'),
      ],
      edges: [{ id: 'e1', source: 'parse', sourceHandle: 'value', target: 'out', targetHandle: 'value' }],
    };
    const codes = validateWorkflow(ir).map((d) => d.code);
    expect(codes).not.toContain('missing_required_input');
  });

  it('accepts a required input supplied by a node literal', () => {
    const base = createWorkflowIR();
    const parse = node(base, 'data.json.parse', 'parse');
    const ir: WorkflowIR = {
      ...base,
      nodes: [
        node(base, 'trigger.manual', 'trig'),
        { ...parse, inputValues: { text: '{"a":1}' } },
        node(base, 'output.result', 'out'),
      ],
      edges: [{ id: 'e1', source: 'parse', sourceHandle: 'value', target: 'out', targetHandle: 'value' }],
    };
    const codes = validateWorkflow(ir).map((d) => d.code);
    expect(codes).not.toContain('missing_required_input');
  });

  it('warns that a wired input ignores its literal', () => {
    const base = createWorkflowIR();
    const parse = node(base, 'data.json.parse', 'parse');
    const ir: WorkflowIR = {
      ...base,
      nodes: [
        node(base, 'trigger.manual', 'trig'),
        { ...parse, inputValues: { text: '{"a":1}' } },
        node(base, 'output.result', 'out'),
      ],
      edges: [
        { id: 'e1', source: 'trig', sourceHandle: 'start', target: 'parse', targetHandle: 'text' },
        { id: 'e2', source: 'parse', sourceHandle: 'value', target: 'out', targetHandle: 'value' },
      ],
    };
    const ignored = validateWorkflow(ir).filter((d) => d.code === 'input_literal_ignored');
    expect(ignored).toHaveLength(1);
  });

  it('accepts an agent objective typed in config without a query wire', () => {
    const base = createWorkflowIR();
    const agent = node(base, 'agent.research', 'agent');
    const ir: WorkflowIR = {
      ...base,
      nodes: [
        node(base, 'trigger.manual', 'trig'),
        { ...agent, config: { ...agent.config, objective: 'Summarize AAPL' } },
        node(base, 'output.result', 'out'),
      ],
      edges: [{ id: 'e1', source: 'agent', sourceHandle: 'finding', target: 'out', targetHandle: 'value' }],
    };
    const errors = validateWorkflow(ir).filter((d) => d.severity === 'error');
    expect(errors).toEqual([]);
  });

  it('rejects an agent with neither a query wire nor a configured objective', () => {
    const base = createWorkflowIR();
    const agent = node(base, 'agent.research', 'agent');
    const ir: WorkflowIR = {
      ...base,
      nodes: [
        node(base, 'trigger.manual', 'trig'),
        { ...agent, config: { ...agent.config, objective: '  ' } },
        node(base, 'output.result', 'out'),
      ],
      edges: [{ id: 'e1', source: 'agent', sourceHandle: 'finding', target: 'out', targetHandle: 'value' }],
    };
    expect(validateWorkflow(ir).map((d) => d.code)).toContain('missing_objective');
  });

  it('rejects a loop without a pinned body workflow', () => {
    const base = createWorkflowIR();
    const loop = { ...node(base, 'flow.for_each', 'loop'), config: { workflowId: '', version: 1 } };
    const ir: WorkflowIR = {
      ...base,
      nodes: [node(base, 'trigger.manual', 'trig'), loop, node(base, 'output.result', 'out')],
      edges: [{ id: 'e1', source: 'loop', sourceHandle: 'results', target: 'out', targetHandle: 'value' }],
    };
    expect(validateWorkflow(ir).map((d) => d.code)).toContain('invalid_subworkflow');
  });

  it('detects cycles', () => {
    const base = createWorkflowIR();
    const a = node(base, 'logic.merge', 'a');
    const b = node(base, 'logic.merge', 'b');
    const ir: WorkflowIR = {
      ...base,
      nodes: [a, b],
      edges: [
        { id: 'e1', source: 'a', sourceHandle: 'value', target: 'b', targetHandle: 'a' },
        { id: 'e2', source: 'b', sourceHandle: 'value', target: 'a', targetHandle: 'a' },
      ],
    };
    expect(validateWorkflow(ir).map((d) => d.code)).toContain('illegal_cycle');
  });

  it('reports invalid config', () => {
    const base = createWorkflowIR();
    const bad = { ...node(base, 'finance.indicator', 'ind'), config: { kind: 'not-real', period: 20 } };
    const ir: WorkflowIR = { ...base, nodes: [node(base, 'trigger.manual', 'trig'), bad] };
    expect(validateWorkflow(ir).map((d) => d.code)).toContain('invalid_config');
  });

  it('rejects switch cases that are not valid JSON', () => {
    const base = createWorkflowIR();
    const sw = { ...node(base, 'logic.switch', 'sw'), config: { cases: '[a, b' } };
    const ir: WorkflowIR = {
      ...base,
      nodes: [node(base, 'trigger.manual', 'trig'), sw, node(base, 'output.result', 'out')],
    };
    expect(validateWorkflow(ir).map((d) => d.code)).toContain('invalid_switch_cases');
  });

  it('warns when a branch uses a case the switch does not configure', () => {
    const base = createWorkflowIR();
    const sw = { ...node(base, 'logic.switch', 'sw'), config: { cases: 'buy, sell' } };
    const gate = node(base, 'flow.gate', 'g');
    const ir: WorkflowIR = {
      ...base,
      nodes: [node(base, 'trigger.manual', 'trig'), sw, gate, node(base, 'output.result', 'out')],
      edges: [
        { id: 'e1', source: 'trig', sourceHandle: 'start', target: 'sw', targetHandle: 'value' },
        { id: 'e2', source: 'sw', sourceHandle: 'case4', target: 'g', targetHandle: 'in' },
      ],
    };
    const warning = validateWorkflow(ir).find((d) => d.code === 'switch_case_not_configured');
    expect(warning?.severity).toBe('warning');
    expect(warning?.nodeId).toBe('sw');
  });

  it('warns when "Any match" and a specific case are both wired', () => {
    const base = createWorkflowIR();
    const sw = { ...node(base, 'logic.switch', 'sw'), config: { cases: 'buy, sell' } };
    const gate = node(base, 'flow.gate', 'g');
    const other = node(base, 'flow.gate', 'g2');
    const ir: WorkflowIR = {
      ...base,
      nodes: [node(base, 'trigger.manual', 'trig'), sw, gate, other, node(base, 'output.result', 'out')],
      edges: [
        { id: 'e1', source: 'trig', sourceHandle: 'start', target: 'sw', targetHandle: 'value' },
        { id: 'e2', source: 'sw', sourceHandle: 'case1', target: 'g', targetHandle: 'in' },
        { id: 'e3', source: 'sw', sourceHandle: 'matched', target: 'g2', targetHandle: 'in' },
      ],
    };
    const warning = validateWorkflow(ir).find((d) => d.code === 'switch_double_branch');
    expect(warning?.severity).toBe('warning');
  });
});
