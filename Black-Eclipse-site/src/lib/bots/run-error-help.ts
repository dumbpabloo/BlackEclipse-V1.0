// ── Plain-language bot run failures ─────────────────────────────────────────
// The runtime records a stable `errorClass` plus a raw reason on every failed
// execution. The reason is usually an upstream message ("fetch failed",
// "401"), which reads as noise. This maps the KNOWN runtime classes to what the
// class means and the concrete next step, so the run detail explains a failure
// instead of only naming it. Unknown classes get a neutral fallback — never a
// guessed cause.

export interface RunErrorHelp {
  /** Short, plain-language meaning of the failure class. */
  what: string;
  /** The concrete thing an operator can change or check. */
  fix: string;
  /** Whether the runtime retries this class by default. */
  retryable: boolean;
}

const HELP: Record<string, RunErrorHelp> = {
  // Transient transport / provider faults (retried by default)
  PROVIDER_TIMEOUT: {
    what: 'The model provider did not answer before the run hit its time limit.',
    fix: 'Run it again. If it repeats, raise the bot\'s max runtime or pick a faster model.',
    retryable: true,
  },
  TOOL_TIMEOUT: {
    what: 'A tool the bot called did not finish in time.',
    fix: 'Retry, or give the tool a smaller scope (fewer symbols / rows) so it finishes sooner.',
    retryable: true,
  },
  PROVIDER_ERROR: {
    what: 'The model provider returned an error.',
    fix: 'Retry. If it keeps failing, check the provider status and the key under Settings › Providers.',
    retryable: true,
  },
  RATE_LIMITED: {
    what: 'The provider or a data source refused the request because of a rate limit.',
    fix: 'Wait and retry, or space the schedule further apart / use a lower-volume model.',
    retryable: true,
  },
  NETWORK_ERROR: {
    what: 'A network call inside the run could not complete.',
    fix: 'Retry. If it persists, check connectivity and any proxy settings.',
    retryable: true,
  },
  INTERNAL_ERROR: {
    what: 'An unexpected runtime error stopped the run.',
    fix: 'Retry once. If it repeats, open the run timeline and share the raw reason — it is recorded verbatim.',
    retryable: true,
  },
  PERSISTENCE_ERROR: {
    what: 'The run could not save its state or output.',
    fix: 'Retry. If the database is out of space or locked, free it and run again.',
    retryable: true,
  },
  QUEUE_UNAVAILABLE: {
    what: 'The run queue was unavailable when the bot fired.',
    fix: 'Retry. The automation keeps its schedule; a manual "Run now" is safe.',
    retryable: true,
  },
  TOOL_ERROR: {
    what: 'A tool the bot called returned an error.',
    fix: 'Retry. If it repeats, check that tool\'s inputs and any data-source key it needs.',
    retryable: true,
  },

  // Configuration / permission (never retried)
  INVALID_CONFIGURATION: {
    what: 'The bot\'s action is not valid for the runtime (missing target, bad parameter).',
    fix: 'Open the bot details and check the action, model and schedule. Fix the field the run names.',
    retryable: false,
  },
  INVALID_CONDITION: {
    what: 'The bot\'s condition expression could not be evaluated.',
    fix: 'Fix or clear the condition in the bot details, then run it again.',
    retryable: false,
  },
  INVALID_INPUT: {
    what: 'The run was given input the action cannot accept.',
    fix: 'Check the bot\'s objective / trigger payload, then run it again.',
    retryable: false,
  },
  INVALID_CREDENTIAL: {
    what: 'A provider or data source rejected the stored credential.',
    fix: 'Re-enter the key under Settings › Providers (or the relevant data source), then retry.',
    retryable: false,
  },
  CREDENTIAL_MISSING: {
    what: 'The run needed a credential that is not configured.',
    fix: 'Add the provider / source key in Settings, then retry.',
    retryable: false,
  },
  PERMISSION_DENIED: {
    what: 'The action was blocked by a permission, not by a transient fault.',
    fix: 'Grant the bot the tool capability it needs (or approve the pending request), then retry.',
    retryable: false,
  },
  TOOL_FORBIDDEN: {
    what: 'The bot called a tool outside its allowed capability set.',
    fix: 'Add that tool to the bot\'s allowed tools, or remove it from the instructions.',
    retryable: false,
  },
  TOOL_VALIDATION_FAILED: {
    what: 'A tool rejected the arguments the bot produced.',
    fix: 'Tighten the bot\'s instructions so it supplies the required fields, then retry.',
    retryable: false,
  },
  INVALID_CREDENTIALS: {
    what: 'A provider or data source rejected the stored credential.',
    fix: 'Re-enter the key under Settings › Providers, then retry.',
    retryable: false,
  },
  WORKFLOW_NOT_FOUND: {
    what: 'The workflow this bot runs no longer exists (or was never saved).',
    fix: 'Re-select the workflow in the bot details, or switch the bot to a direct agent action.',
    retryable: false,
  },
  TASK_NOT_FOUND: {
    what: 'The task or skill this bot targets no longer exists.',
    fix: 'Re-select the target in the bot details, then retry.',
    retryable: false,
  },
  AGENT_NOT_FOUND: {
    what: 'The agent this bot delegates to no longer exists.',
    fix: 'Re-select the agent in the bot details, then retry.',
    retryable: false,
  },
  BUSINESS_RULE_FAILURE: {
    what: 'A deliberate guardrail refused the action (for example a trading or write rule).',
    fix: 'Nothing is broken. Adjust the bot so it stays inside the rule, or act manually.',
    retryable: false,
  },
  APPROVAL_DENIED: {
    what: 'An operator declined the approval this run was waiting on.',
    fix: 'Review why it was denied, then re-approve or change the bot before the next run.',
    retryable: false,
  },
  RESOURCE_LIMIT_EXCEEDED: {
    what: 'The run finished, but it used more tokens or cost than the bot\'s budget allows.',
    fix: 'Raise the token/cost limit in the bot details, or narrow the task so it uses less.',
    retryable: false,
  },

  // Team / workflow / graph bridges
  TEAM_INCOMPLETE: {
    what: 'The committee team finished without a complete decision.',
    fix: 'Check the team status in the run summary, then retry. A partial result is reported honestly.',
    retryable: false,
  },
  TEAM_ERROR: {
    what: 'The team runtime failed before it could produce a decision.',
    fix: 'Retry. If it repeats, check the team preset and the agents it references.',
    retryable: false,
  },
  TEAM_UNAVAILABLE: {
    what: 'The team runtime was not available for this run.',
    fix: 'Restart the Python agent runtime, then retry.',
    retryable: false,
  },
  RUNTIME_UNAVAILABLE: {
    what: 'The Python agent runtime was not reachable, so nothing ran.',
    fix: 'Start/restart the agent runtime (or run Doctor), then retry. Runs fail honestly rather than faking a reply.',
    retryable: false,
  },
  RUNTIME_STARTING: {
    what: 'The Python agent runtime is still starting. Nothing has failed.',
    fix: 'Wait a few seconds and retry — the message is kept. A cold start normally finishes in under a minute.',
    retryable: true,
  },
  GRAPH_RUN_FAILED: {
    what: 'The graph workflow ran but ended in a failure state.',
    fix: 'Open the workflow run to see the failing node, fix it in the graph, then retry.',
    retryable: false,
  },
  GRAPH_RUN_TIMEOUT: {
    what: 'The graph workflow did not finish within the runtime limit.',
    fix: 'Raise the automation max runtime, or make the graph finish sooner.',
    retryable: false,
  },
  GRAPH_RUN_REJECTED: {
    what: 'The runtime rejected the graph run request (bad input or unavailable workflow).',
    fix: 'Check the workflow is saved and the message input matches its Start node, then retry.',
    retryable: false,
  },
  GRAPH_BRIDGE_ERROR: {
    what: 'The bridge between the bot and the graph runtime failed.',
    fix: 'Restart the agent runtime and retry.',
    retryable: false,
  },
  ORCHESTRATION_ERROR: {
    what: 'The orchestration layer failed while assembling the work.',
    fix: 'Retry. If it repeats, check the bot\'s action and the runtime health.',
    retryable: false,
  },
  TASK_ERROR: {
    what: 'The task system could not create or update the run\'s task.',
    fix: 'Retry. If it repeats, check the task runtime is healthy.',
    retryable: false,
  },

  // Terminal states that are not faults
  CANCELLED: {
    what: 'The run was stopped on purpose.',
    fix: 'No action needed — run it again if you still want the result.',
    retryable: false,
  },
  INTERRUPTED: {
    what: 'The run was interrupted (app restart or worker shutdown).',
    fix: 'Run it again; the automation\'s schedule is unchanged.',
    retryable: false,
  },
};

const FALLBACK: RunErrorHelp = {
  what: 'The run stopped with a failure class the UI does not have a specific note for.',
  fix: 'Read the raw reason above and the run timeline; the runtime records what actually happened.',
  retryable: false,
};

/** Explain a run's stable error class. Never invents a cause for unknown codes. */
export function runErrorHelp(errorClass: string | null | undefined): RunErrorHelp | null {
  if (!errorClass) return null;
  return HELP[errorClass] ?? FALLBACK;
}
