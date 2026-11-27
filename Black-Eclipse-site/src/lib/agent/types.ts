/**
 * Agent provider abstraction.
 *
 * BlackEclipse's chat is provider-agnostic: any LLM provider can be plugged
 * in by implementing this interface. Without a configured provider, the app
 * falls back to a built-in mock so the UI always works.
 */

export interface ChatMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

export interface AgentTool {
  name: string;
  description: string;
  execute: (args: Record<string, unknown>) => Promise<string>;
}

/** A live step the agent is taking, surfaced to the UI while a run is in flight. */
export interface AgentStatusEvent {
  /** Coarse phase for the working indicator. */
  phase: "starting" | "thinking" | "tool" | "writing" | "done";
  /** Human-readable label, e.g. "Checking AAPL price…". */
  label: string;
  /** Raw harness event type that produced this. */
  eventType: string;
  /** Tool name, when the phase is `tool`. */
  tool?: string;
}

/**
 * The UI the user is currently focused on, forwarded with a run so the agent can
 * resolve "my widgets"/"this page" to a concrete blueprint. Presentation context
 * only — it grants no capability and is truncated at the front door.
 */
export interface UiFocus {
  page?: { blueprintId?: string; title?: string; path?: string };
}

export interface ChatOptions {
  /**
   * Agent preset id that answers this turn. Ordinary chat omits it (the front
   * door resolves `eclipse-agent`); a bot chat passes the bot's own brain so the
   * preset's prompt/tools apply exactly as they do on a scheduled run.
   */
  agentId?: string;
  /**
   * Conversation id. When present the front door loads that conversation's
   * workspace folder as run context and saves the answer back into it.
   */
  chatId?: string;
  /**
   * Server-owned workspace root id for this turn. `bot:<id>` is the one
   * server-derived form (the runtime creates it under `.black-eclipse/bots/`);
   * an approved root id is also accepted. Never a raw path.
   */
  workspaceRootId?: string;
  /**
   * Opt this conversation into PRIVATE per-conversation long-term memory. Set
   * by bot chats and group-room member turns, where each conversation keeps its
   * own durable memory isolated from every other. Absent for ordinary chat.
   */
  memoryScope?: 'conversation';
  /**
   * The model id the operator picked in the composer for THIS turn. Overrides
   * the saved model in the settings vault for the provider already configured
   * (the provider/key are unchanged). Absent = the saved model.
   */
  model?: string;
  /**
   * Provider for a bot's pinned model. A bot may pin provider + model; the
   * scheduled run uses both, so the chat turn forwards both too. Credentials
   * still come from the runtime's configured provider (the same rule the
   * automation dispatcher applies).
   */
  modelProvider?: string;
  /**
   * The reasoning-effort ("model intelligence") the operator picked for THIS
   * turn. Overrides the saved default for the run. Absent = Auto, so the saved
   * default and then the runtime's task profile decide.
   */
  reasoningEffort?: import('./reasoning').ReasoningLevel;
  /**
   * The composer's quant-mode preference for THIS turn. "on" opts the turn into
   * the quant protocol, "off" suppresses it, "auto" (default) lets the runtime's
   * deterministic classifier decide. It is guidance, never a capability grant.
   */
  quantMode?: import('./quant-mode').QuantMode;
  /**
   * The composer's choices-mode preference for THIS turn. "on" nudges the model
   * to ask one question with clickable options before starting an ambiguous
   * turn, "off" suppresses that nudge, "auto" (default) lets the base prompt
   * decide. Presentation guidance only, never a capability grant.
   */
  choicesMode?: import('./choices-mode').ChoicesMode;
  /**
   * The composer's plan-mode preference for THIS turn. "on" tells the model to
   * lay out a concrete plan and wait for approval before starting the work,
   * "off" suppresses that nudge, "auto" (default) lets the base guidance decide.
   * Presentation guidance only, never a capability grant or a hard block.
   */
  planMode?: import('./plan-mode').PlanMode;
  /**
   * The UI page the user is currently focused on (Pages / UI Builder), forwarded
   * into the run's context so `ui_get_current_page` can resolve "my widgets".
   */
  uiContext?: UiFocus;
  /**
   * Continue an existing run from its latest durable checkpoint instead of
   * starting a new one. Set by Retry on a failed turn: the runtime re-enters the
   * SAME run id and reuses every completed tool call, so a 10-minute research
   * turn is not thrown away by one timeout. The front door falls back to a
   * fresh run when the checkpoint is unusable.
   */
  resumeRunId?: string;
  /**
   * Called with the harness run id as soon as it is known, so the caller can
   * offer cancellation before the reply arrives.
   */
  onRunId?: (runId: string) => void;
  /** Reference images attached to a bot; sent as real image parts. */
  images?: import('@/lib/harness/types').RunImageInput[];
  /** Called with each streamed token fragment as the answer is written. */
  onDelta?: (text: string) => void;
  /** Called with each coarse agent step, so the UI shows it is working. */
  onStatus?: (event: AgentStatusEvent) => void;
  /**
   * Called with each normalized UI event (tool start/end, retrieval activity,
   * final, error, status). Superset of `onDelta`/`onStatus` — the shared
   * `useAgentRun` hook uses this to build the full response lifecycle.
   */
  onEvent?: (event: import('./events').HarnessUiEvent) => void;
  /** Abort the underlying fetch (used by Stop generation). */
  signal?: AbortSignal;
}

export interface AgentProvider {
  readonly id: string;
  readonly label: string;
  /** Returns true if this provider is configured (has API key, etc.). */
  isConfigured(): boolean;
  /** Send a chat and get a reply. May use tools. */
  chat(messages: ChatMessage[], tools?: AgentTool[], options?: ChatOptions): Promise<string>;
}
