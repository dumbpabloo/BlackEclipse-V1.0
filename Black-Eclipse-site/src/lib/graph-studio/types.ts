// ── Graph Studio — shared types ─────────────────────────────────────────────
// The vocabulary of the visual graph-engineering studio. The CANONICAL workflow
// representation (the "Eclipse Workflow IR") is defined in `ir.ts`; this file
// holds the plain TypeScript types so non-React consumers (validation, patches,
// compiler, server) can import them without pulling in the zod runtime.
//
// Two layers are deliberately kept apart:
//   * PERSISTENT graph definition — ids, node types/versions, config, ports and
//     edges. This is what is versioned and (later) compiled/executed.
//   * TRANSIENT editor state — selection, hover, viewport, dirty flag and the
//     in-progress undo/redo stack. Never persisted as workflow semantics.
//
// Node layout (x/y) is persisted WITH the document because a graph that reopens
// is useless without it, but it is explicitly NON-SEMANTIC: the compiler ignores
// `layout`, so moving a node can never change what a workflow does.

/** The current canonical IR schema version. Bump on any breaking IR change and
 *  add a migration in `migrations.ts`. */
export const WORKFLOW_IR_VERSION = 2;

// ── Ports ────────────────────────────────────────────────────────────────────

/**
 * A typed data port. `control` is a distinct KIND (see `PortKind`) used for
 * explicit control-flow wiring (branch/join), never for carrying values.
 */
export type PortType =
  | 'number'
  | 'boolean'
  | 'string'
  | 'object'
  | 'array'
  | 'datetime'
  // A broker order/position id: an MT5 integer or a REST venue's opaque id.
  | 'ticket'
  // Finance-native types
  | 'instrument'
  | 'ohlcv'
  | 'indicator'
  | 'signal'
  | 'portfolio'
  | 'order_intent'
  | 'research_finding'
  | 'evidence'
  | 'agent_result'
  | 'execution_result'
  // Local asset references (graph asset store / vision asset store). They carry
  // a small metadata object, never the bytes themselves.
  | 'file'
  | 'image'
  | 'any';

/** Control ports gate execution order; data ports carry values. */
export type PortKind = 'data' | 'control';

export interface PortDef {
  /** Stable handle id within the node (unique per direction + node type). */
  id: string;
  /** Human label shown on the canvas. */
  name: string;
  /** The value type this port carries. `control` ports set `kind: 'control'`. */
  type: PortType;
  kind?: PortKind;
  /** Optional inputs may be left unconnected with no validation error. */
  optional?: boolean;
  /** May explicitly be null even when connected. */
  nullable?: boolean;
  description?: string;
}

// ── Nodes & edges ────────────────────────────────────────────────────────────

export interface NodePosition {
  x: number;
  y: number;
}

/** One node in the graph. `type` + `version` resolve to a registered
 *  `NodeDefinition` (see `registry.ts`). `config` is validated against that
 *  definition's schema; it is never trusted from storage. */
export interface WorkflowNode {
  id: string;
  /** Stable node-type identifier from the registry. */
  type: string;
  /** Node-type version this node was authored against. */
  version: string;
  /** Optional user label overriding the definition's display name. */
  label?: string;
  /** Free-form operator note, saved with the workflow (not executed). */
  notes?: string;
  config: Record<string, unknown>;
  /**
   * Literal values for input ports, keyed by port id. A wire always wins; a
   * literal lets a node be self-contained (e.g. Parse JSON carrying its text).
   * Control ports never take literals.
   */
  inputValues?: Record<string, unknown>;
  /** Non-semantic canvas position (persisted, ignored by the compiler). */
  layout: NodePosition;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  sourceHandle: string;
  target: string;
  targetHandle: string;
  /** Optional declarative branch condition (control flow). Never code. */
  condition?: string;
}

// ── Execution settings ───────────────────────────────────────────────────────

/**
 * Operating mode. `live` is never selectable from the editor without the
 * separate authorization Phase 8 requires; the editor treats it as read-only
 * unless explicitly enabled by the runtime.
 */
export type ExecutionMode = 'research' | 'backtest' | 'paper' | 'live';

export interface WorkflowSettings {
  executionMode: ExecutionMode;
  /** Stop the whole run at the first node error. */
  failFast: boolean;
  /** Upper bound on concurrently-running nodes. */
  maxConcurrency: number;
  /** Graph-level wall-clock budget in ms (0 = no explicit budget). */
  timeoutMs: number;
}

// ── The canonical document ───────────────────────────────────────────────────

export interface WorkflowMeta {
  createdAt: string;
  updatedAt: string;
  tags: string[];
}

export interface WorkflowIR {
  irVersion: number;
  id: string;
  name: string;
  description: string;
  meta: WorkflowMeta;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  settings: WorkflowSettings;
  /** Capabilities the graph as a whole requires (union of node capabilities). */
  requiredCapabilities: string[];
}

// ── Node definitions (registry) ──────────────────────────────────────────────

export type NodeCategory =
  | 'trigger'
  | 'data'
  | 'transform'
  | 'logic'
  | 'integration'
  | 'finance'
  | 'quant'
  | 'agent'
  | 'skills'
  | 'research'
  | 'trading'
  | 'learning'
  | 'decision'
  | 'media'
  | 'output';

/**
 * How dangerous a node is if executed. The runtime — never the editor — decides
 * whether a given mode may run a node with a given class. `trading` nodes are
 * hard-gated to paper/live and can never run in research/backtest.
 */
export type SideEffectClass =
  | 'none'
  | 'read'
  | 'write'
  | 'network'
  | 'trading'
  /** A model-provider call: costs tokens, does not act on the world. A test run
   *  may perform it with a narrowed tool surface. */
  | 'model'
  /** Local scratch (the runtime's own store). Survives the run, cannot affect
   *  the world, so a test run may perform it. */
  | 'local_write';

/**
 * How a config key is presented and edited. `inline` fields are the 1–2 values
 * shown directly on the canvas node; everything else lives in the inspector.
 * This is presentation metadata only — it never changes the persisted config or
 * the node's semantics.
 */
export interface ConfigFieldDef {
  key: string;
  label: string;
  kind: 'text' | 'number' | 'boolean' | 'select' | 'json'
    | 'textarea' | 'keyvalue' | 'code' | 'combobox' | 'slider' | 'file' | 'image'
    | 'images';
  /**
   * For `keyvalue`: the persisted value is a JSON *string*, not an object.
   * Gives a legacy string-typed field (e.g. a rename map) the row editor
   * without changing the stored contract.
   */
  jsonString?: boolean;
  /**
   * The input port this field shadows when wired, when it differs from `key`.
   * `agent.research` is the canonical case: the canvas field is `objective`
   * but the wire that supplies it is the `query` port.
   */
  port?: string;
  /** Show on the node card (at most two per definition; json is panel-only). */
  inline?: boolean;
  /**
   * Priority for the card's inline slots (1 = highest). A field with `quick`
   * is eligible for an inline slot even when `inline` is not set; ties fall
   * back to declaration order. Presentation only.
   */
  quick?: number;
  options?: { value: string; label: string }[];
  placeholder?: string;
  min?: number;
  max?: number;
  step?: number;
  /** A one-line explanation shown under the labelled control. */
  help?: string;
  /** Marks the parameter as required in the inspector (validation still owns it). */
  required?: boolean;
  /** Optional section heading in the inspector. */
  group?: string;
}

export interface NodeDefinition {
  type: string;
  version: string;
  category: NodeCategory;
  label: string;
  description: string;
  /** Icon key resolved by the UI; never an import. */
  icon?: string;
  inputs: PortDef[];
  outputs: PortDef[];
  /** Runtime validator for `config`. Always re-run at the trust boundary. */
  configSchema: unknown;
  defaultConfig: () => Record<string, unknown>;
  /** Capabilities this node requires to run (checked against the account). */
  capabilities: string[];
  sideEffects: SideEffectClass;
  /** Modes in which this node is allowed to execute. */
  allowedModes: ExecutionMode[];
  /** Whether this node is a valid graph terminal (e.g. output nodes). */
  terminal?: boolean;
  docs?: string;
}

// ── Diagnostics ──────────────────────────────────────────────────────────────

export type DiagnosticSeverity = 'error' | 'warning';

export interface Diagnostic {
  severity: DiagnosticSeverity;
  code: string;
  message: string;
  /** The node or edge this diagnostic refers to, when applicable. */
  nodeId?: string;
  edgeId?: string;
}
