// ── Graph validation — independent of the frontend ───────────────────────────
// A pure function over the canonical IR + the node registry. It is the SAME
// code the editor runs live and (Phase 3) the compiler runs before building an
// execution plan, so a graph that validates in the canvas cannot be rejected by
// the compiler for a structural reason.
//
// Every diagnostic names the exact node id and/or edge id it refers to, so the
// UI can focus the offending element instead of showing a wall of text.

import type { z } from 'zod';
import type {
  Diagnostic,
  NodeDefinition,
  PortDef,
  WorkflowIR,
  WorkflowNode,
} from './types';
import { getNodeDefinition } from './registry';
import { parseSwitchCases } from './node-ports';

/** A literal counts when it is present, including an explicit `null` or `false`. */
export function hasInputLiteral(node: WorkflowNode, portId: string): boolean {
  return Object.prototype.hasOwnProperty.call(node.inputValues ?? {}, portId);
}

/**
 * Nodes whose required data port may instead be supplied by a config field.
 * Keep this in lockstep with the Python compiler and the node executors.
 */
const CONFIG_FALLBACK: Record<string, { port: string; key: string }> = {
  'data.json.parse': { port: 'text', key: 'text' },
  'data.dataset.save': { port: 'value', key: 'valueJson' },
  'data.dataset.load': { port: 'ref', key: 'ref' },
};

/** True when the node's config carries a usable fallback for this data port. */
export function hasConfigFallback(node: WorkflowNode, portId: string): boolean {
  const rule = CONFIG_FALLBACK[node.type];
  if (!rule || rule.port !== portId) return false;
  const value = node.config[rule.key];
  return typeof value === 'string' && value.trim() !== '';
}

/**
 * Can a value typed by `source` flow into `target`?
 *   * control ports only connect to control ports (never to a data port);
 *   * `any` is compatible with everything;
 *   * otherwise the types must match exactly (no silent coercion).
 */
export function portsCompatible(source: PortDef, target: PortDef): boolean {
  const sourceControl = source.kind === 'control';
  const targetControl = target.kind === 'control';
  if (sourceControl || targetControl) return sourceControl && targetControl;
  if (source.type === 'any' || target.type === 'any') return true;
  // A ticket is an id: MT5 supplies a number, a REST venue an opaque string.
  if (source.type === 'ticket' || target.type === 'ticket') {
    const pair = new Set([source.type, target.type]);
    return pair.size === 1
      || (pair.has('ticket')
        && [...pair].every((type) => type === 'ticket' || type === 'number' || type === 'string'));
  }
  return source.type === target.type;
}

/** Validate a node's config against its definition's schema. */
export function validateConfig(def: NodeDefinition, config: unknown): string | null {
  const schema = def.configSchema as z.ZodType;
  const result = schema.safeParse(config ?? {});
  if (result.success) return null;
  const issue = result.error.issues[0];
  return `${issue?.path.join('.') || 'config'}: ${issue?.message ?? 'invalid'}`;
}

/** The union of every registered node's declared capabilities. */
export function requiredCapabilitiesFor(ir: WorkflowIR): string[] {
  const set = new Set<string>();
  for (const node of ir.nodes) {
    const def = getNodeDefinition(node.type, node.version);
    if (!def) continue;
    for (const cap of def.capabilities) set.add(cap);
  }
  return [...set].sort();
}

function buildAdjacency(ir: WorkflowIR): Map<string, Set<string>> {
  const adj = new Map<string, Set<string>>();
  for (const node of ir.nodes) adj.set(node.id, new Set());
  for (const edge of ir.edges) {
    adj.get(edge.source)?.add(edge.target);
  }
  return adj;
}

/** Detect any cycle (Phase 1 requires a DAG; loops are Phase 4 constructs). */
function findCycle(ir: WorkflowIR): string[] | null {
  const adj = buildAdjacency(ir);
  const WHITE = 0, GRAY = 1, BLACK = 2;
  const color = new Map<string, number>();
  for (const node of ir.nodes) color.set(node.id, WHITE);
  const stack: string[] = [];
  let cycle: string[] | null = null;

  const visit = (id: string): boolean => {
    color.set(id, GRAY);
    stack.push(id);
    for (const next of adj.get(id) ?? []) {
      if (color.get(next) === GRAY) {
        const idx = stack.indexOf(next);
        cycle = [...stack.slice(idx), next];
        return true;
      }
      if (color.get(next) === WHITE && visit(next)) return true;
    }
    stack.pop();
    color.set(id, BLACK);
    return false;
  };

  for (const node of ir.nodes) {
    if (color.get(node.id) === WHITE && visit(node.id)) return cycle;
  }
  return null;
}

function sourcePort(def: NodeDefinition | undefined, handle: string): PortDef | undefined {
  return def?.outputs.find((p) => p.id === handle);
}
function targetPort(def: NodeDefinition | undefined, handle: string): PortDef | undefined {
  return def?.inputs.find((p) => p.id === handle);
}

/**
 * Validate a workflow. Returns every diagnostic found (never throws). An empty
 * array means the graph is structurally sound and safe to compile.
 */
export function validateWorkflow(ir: WorkflowIR): Diagnostic[] {
  const diagnostics: Diagnostic[] = [];
  const byId = new Map(ir.nodes.map((n) => [n.id, n]));

  // Duplicate identifiers.
  const seenNodes = new Set<string>();
  for (const node of ir.nodes) {
    if (seenNodes.has(node.id)) {
      diagnostics.push({ severity: 'error', code: 'duplicate_node_id', message: `Duplicate node id "${node.id}".`, nodeId: node.id });
    }
    seenNodes.add(node.id);
  }
  const seenEdges = new Set<string>();
  for (const edge of ir.edges) {
    if (seenEdges.has(edge.id)) {
      diagnostics.push({ severity: 'error', code: 'duplicate_edge_id', message: `Duplicate edge id "${edge.id}".`, edgeId: edge.id });
    }
    seenEdges.add(edge.id);
  }

  const nodeDefs = new Map<string, NodeDefinition | undefined>();

  // Per-node checks.
  for (const node of ir.nodes) {
    const def = getNodeDefinition(node.type, node.version);
    nodeDefs.set(node.id, def);
    if (!def) {
      diagnostics.push({
        severity: 'error',
        code: 'missing_node_type',
        message: `Node type "${node.type}" @ ${node.version} is not registered.`,
        nodeId: node.id,
      });
      continue;
    }
    const configError = validateConfig(def, node.config);
    if (configError) {
      diagnostics.push({
        severity: 'error',
        code: 'invalid_config',
        message: `Invalid config for "${node.id}": ${configError}.`,
        nodeId: node.id,
      });
    }
    if (def.type === 'flow.subworkflow'
      || def.type === 'flow.for_each'
      || def.type === 'flow.repeat'
      || def.type === 'flow.while') {
      const workflowId = typeof node.config.workflowId === 'string' ? node.config.workflowId.trim() : '';
      const version = Number(node.config.version);
      if (!workflowId || !Number.isInteger(version) || version < 1) {
        diagnostics.push({
          severity: 'error',
          code: 'invalid_subworkflow',
          message: `Node "${node.id}" needs a pinned subworkflow (workflowId and version >= 1).`,
          nodeId: node.id,
        });
      }
    }
    if (def.type === 'logic.switch') {
      // The runtime refuses a `cases` text that is not valid JSON, so surface it
      // in the editor instead of at run time.
      if (parseSwitchCases(node.config.cases) === null) {
        diagnostics.push({
          severity: 'error',
          code: 'invalid_switch_cases',
          message: `Node "${node.id}" has switch cases that are not valid JSON — use a comma list (a, b, c) or a JSON array (["a,b", "c"]).`,
          nodeId: node.id,
        });
      }
    }
    if (def.sideEffects === 'trading' && !['paper', 'live'].includes(ir.settings.executionMode)) {
      diagnostics.push({
        severity: 'error',
        code: 'invalid_execution_mode',
        message: `Node "${node.id}" places trades and cannot run in ${ir.settings.executionMode} mode.`,
        nodeId: node.id,
      });
    }
    if (!def.allowedModes.includes(ir.settings.executionMode)) {
      diagnostics.push({
        severity: 'error',
        code: 'invalid_execution_mode',
        message: `Node "${node.id}" (${def.label}) is not permitted in ${ir.settings.executionMode} mode.`,
        nodeId: node.id,
      });
    }
  }

  // Per-edge checks.
  const connectedInputs = new Set<string>(); // `${nodeId}:${handle}`
  for (const edge of ir.edges) {
    const src = byId.get(edge.source);
    const tgt = byId.get(edge.target);
    if (!src || !tgt) {
      diagnostics.push({
        severity: 'error',
        code: 'invalid_edge_endpoints',
        message: `Edge "${edge.id}" references a missing node.`,
        edgeId: edge.id,
      });
      continue;
    }
    const srcDef = nodeDefs.get(edge.source);
    const tgtDef = nodeDefs.get(edge.target);
    const outPort = sourcePort(srcDef, edge.sourceHandle);
    const inPort = targetPort(tgtDef, edge.targetHandle);
    if (!outPort || !inPort) {
      diagnostics.push({
        severity: 'error',
        code: 'invalid_edge_handles',
        message: `Edge "${edge.id}" references an unknown port.`,
        edgeId: edge.id,
      });
      continue;
    }
    if (!portsCompatible(outPort, inPort)) {
      diagnostics.push({
        severity: 'error',
        code: 'incompatible_ports',
        message: `Cannot connect ${outPort.type} → ${inPort.type} (edge "${edge.id}").`,
        edgeId: edge.id,
      });
    }
    connectedInputs.add(`${edge.target}:${edge.targetHandle}`);
    if (srcDef?.type === 'logic.switch') {
      const match = /^case(\d+)$/.exec(edge.sourceHandle);
      const cases = parseSwitchCases(src.config.cases);
      if (match && cases && Number(match[1]) > cases.length) {
        diagnostics.push({
          severity: 'warning',
          code: 'switch_case_not_configured',
          message: `Edge "${edge.id}" uses ${edge.sourceHandle}, but node "${src.id}" configures only ${cases.length} case(s). Add the case or rewire the branch.`,
          edgeId: edge.id,
          nodeId: src.id,
        });
      }
    }
  }

  // A Switch can fire "Any match" AND the specific case at the same time, so
  // wiring both makes two branches run. Say so before the run surprises someone.
  for (const node of ir.nodes) {
    if (nodeDefs.get(node.id)?.type !== 'logic.switch') continue;
    const handles = new Set(
      ir.edges.filter((edge) => edge.source === node.id).map((edge) => edge.sourceHandle),
    );
    if (handles.has('matched') && [...handles].some((handle) => /^case\d+$/.test(handle))) {
      diagnostics.push({
        severity: 'warning',
        code: 'switch_double_branch',
        message: `Node "${node.id}" wires both "Any match" and a specific case; both branches run when that case matches. Wire only one shape.`,
        nodeId: node.id,
      });
    }
  }

  // Required inputs must be connected OR carry a literal on the node.
  for (const node of ir.nodes) {
    const def = nodeDefs.get(node.id);
    if (!def) continue;
    for (const input of def.inputs) {
      // Required CONTROL inputs count too (a gate must be triggered by
      // something); optional ports of either kind may stay unwired.
      if (input.optional) continue;
      if (connectedInputs.has(`${node.id}:${input.id}`)) continue;
      if (input.kind !== 'control' && hasInputLiteral(node, input.id)) continue;
      if (input.kind !== 'control' && hasConfigFallback(node, input.id)) continue;
      diagnostics.push({
        severity: 'error',
        code: 'missing_required_input',
        message: `Node "${node.id}" is missing required input "${input.name}".`,
        nodeId: node.id,
      });
    }
  }

  // A literal on a wired port is ignored (the wire wins) — say so rather than
  // leaving a stale value that looks effective.
  for (const node of ir.nodes) {
    const def = nodeDefs.get(node.id);
    if (!def || !node.inputValues) continue;
    for (const portId of Object.keys(node.inputValues)) {
      const port = def.inputs.find((p) => p.id === portId);
      if (!port) {
        diagnostics.push({
          severity: 'warning',
          code: 'unknown_input_literal',
          message: `Node "${node.id}" carries a value for unknown input "${portId}".`,
          nodeId: node.id,
        });
        continue;
      }
      if (connectedInputs.has(`${node.id}:${portId}`)) {
        diagnostics.push({
          severity: 'warning',
          code: 'input_literal_ignored',
          message: `Node "${node.id}" input "${port.name}" is wired; its literal value is ignored.`,
          nodeId: node.id,
        });
      }
    }
  }

  // Nodes whose required value may come EITHER from a wire OR from config. The
  // port is optional so a config-only node is valid; this check enforces that
  // at least one source is actually present (the executor uses the same rule).
  const INPUT_OR_CONFIG: Record<string, { port: string; key: string; label: string }> = {
    'agent.research': { port: 'query', key: 'objective', label: 'a query or an objective' },
    'agent.plan': { port: 'objective', key: 'objective', label: 'an objective' },
  };
  for (const node of ir.nodes) {
    const rule = INPUT_OR_CONFIG[node.type];
    if (!rule) continue;
    const wired = connectedInputs.has(`${node.id}:${rule.port}`);
    const configured = node.config[rule.key];
    if (!wired && (typeof configured !== 'string' || configured.trim() === '')) {
      diagnostics.push({
        severity: 'error',
        code: 'missing_objective',
        message: `Node "${node.id}" needs ${rule.label}: wire "${rule.port}" or type it in the node settings.`,
        nodeId: node.id,
      });
    }
  }

  // Cycles (DAG required in Phase 1).
  const cycle = findCycle(ir);
  if (cycle) {
    diagnostics.push({
      severity: 'error',
      code: 'illegal_cycle',
      message: `The graph contains a cycle: ${cycle.join(' → ')}.`,
      nodeId: cycle[0],
    });
  }

  // Reachability + terminal paths.
  if (ir.nodes.length > 0) {
    const adj = buildAdjacency(ir);
    const triggers = ir.nodes.filter((n) => nodeDefs.get(n.id)?.category === 'trigger');
    const reachable = new Set<string>();
    const queue = triggers.map((n) => n.id);
    for (const id of queue) reachable.add(id);
    while (queue.length > 0) {
      const id = queue.shift() as string;
      for (const next of adj.get(id) ?? []) {
        if (!reachable.has(next)) {
          reachable.add(next);
          queue.push(next);
        }
      }
    }
    if (triggers.length === 0) {
      diagnostics.push({
        severity: 'error',
        code: 'missing_trigger',
        message: 'The graph has no trigger node, so it can never start.',
      });
    }
    for (const node of ir.nodes) {
      if (!reachable.has(node.id)) {
        diagnostics.push({
          severity: 'warning',
          code: 'unreachable_node',
          message: `Node "${node.id}" is not reachable from any trigger.`,
          nodeId: node.id,
        });
      }
    }
    const terminals = ir.nodes.filter((n) => nodeDefs.get(n.id)?.terminal);
    if (terminals.length === 0) {
      diagnostics.push({
        severity: 'error',
        code: 'missing_terminal',
        message: 'The graph has no terminal (output) node.',
      });
    } else if (!terminals.some((t) => reachable.has(t.id))) {
      diagnostics.push({
        severity: 'warning',
        code: 'unreachable_terminal',
        message: 'No terminal node is reachable from a trigger.',
      });
    }
  }

  // Capability consistency (internal check; authorization is enforced runtime-side).
  const required = requiredCapabilitiesFor(ir);
  const declared = new Set(ir.requiredCapabilities);
  for (const cap of required) {
    if (!declared.has(cap)) {
      diagnostics.push({
        severity: 'warning',
        code: 'undeclared_capability',
        message: `Required capability "${cap}" is not declared on the workflow.`,
      });
    }
  }

  return diagnostics;
}

/** Convenience: does the graph have any error-severity diagnostic? */
export function hasErrors(diagnostics: Diagnostic[]): boolean {
  return diagnostics.some((d) => d.severity === 'error');
}
