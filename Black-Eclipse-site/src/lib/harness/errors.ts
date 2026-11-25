// ─── Structured harness errors ───────────────────────────────────────────────
// Every failure in the runtime carries a stable machine-readable `code` so the
// API can map it to an HTTP status and the UI can branch without string
// matching. Messages never include secrets — providers are responsible for
// not leaking keys, and `safeMessage` exists so persistence can redact.

export type HarnessErrorCode =
  | "INVALID_INPUT"
  | "RUNTIME_UNAVAILABLE"
  // A cold runtime boot is in progress. Transient by definition: the same
  // request succeeds once the boot finishes, so the client retries it instead
  // of telling the person to install anything.
  | "RUNTIME_STARTING"
  | "PROVIDER_NOT_CONFIGURED"
  | "PROVIDER_ERROR"
  | "PROVIDER_TIMEOUT"
  | "TOOL_NOT_FOUND"
  | "TOOL_VALIDATION_FAILED"
  | "TOOL_FORBIDDEN"
  | "TOOL_TIMEOUT"
  | "TOOL_ERROR"
  | "MAX_STEPS_EXCEEDED"
  | "TOOL_BUDGET_EXCEEDED"
  | "CANCELLED"
  | "PERSISTENCE_ERROR"
  // Surface guardrails (v3 P3): the finished answer broke a rule the
  // activated surface set, so the run failed instead of shipping the answer.
  | "GUARDRAIL_TOOL_REQUIRED"
  | "GUARDRAIL_CITATION_MISSING"
  | "GUARDRAIL_ANSWER_TOO_LONG"
  | "INTERNAL_ERROR";

export interface HarnessErrorJson {
  code: HarnessErrorCode;
  message: string;
  /** Optional, non-sensitive diagnostic detail (already redacted). */
  detail?: string;
}

/** Redact anything that looks like a bearer token / API key from a message. */
export function redactSecrets(text: string): string {
  return text
    .replace(/(Bearer\s+)[A-Za-z0-9._\-]+/gi, "$1[redacted]")
    .replace(/(sk-[A-Za-z0-9._\-]{8,})/g, "[redacted-key]")
    .replace(/("?(?:api[_-]?key|token|secret|authorization)"?\s*[:=]\s*")([^"]+)(")/gi, "$1[redacted]$3");
}

export class HarnessError extends Error {
  readonly code: HarnessErrorCode;
  readonly detail?: string;

  constructor(code: HarnessErrorCode, message: string, detail?: string) {
    super(message);
    this.name = "HarnessError";
    this.code = code;
    this.detail = detail;
  }

  toJSON(): HarnessErrorJson {
    return {
      code: this.code,
      message: redactSecrets(this.message),
      ...(this.detail ? { detail: redactSecrets(this.detail) } : {}),
    };
  }
}

/**
 * A non-2xx answer from the Python runtime, carrying the HTTP status.
 *
 * Why: the runtime reports "not found" as `404` with a human `detail` string
 * (e.g. "Run not found"). Callers used to detect that by matching the status
 * number inside the message, which only worked when the runtime sent no
 * detail — so a missing run surfaced as a 500 instead of a 404. The status
 * travels on the error itself; the message stays exactly what the runtime
 * said, so nothing user-facing changes.
 */
export class HarnessHttpError extends Error {
  readonly status: number;
  readonly detail?: string;

  constructor(status: number, message: string, detail?: string) {
    super(message);
    this.name = "HarnessHttpError";
    this.status = status;
    this.detail = detail;
  }
}

/** Whether a thrown value means "the runtime does not have it" (HTTP 404). */
export function isNotFoundError(err: unknown): boolean {
  if (err instanceof HarnessHttpError) return err.status === 404;
  if (err instanceof Error) return /\b404\b/.test(err.message);
  return false;
}

/** Coerce any thrown value into a HarnessError-shaped record for persistence. */
export function toHarnessErrorJson(err: unknown, fallbackCode: HarnessErrorCode = "INTERNAL_ERROR"): HarnessErrorJson {
  if (err instanceof HarnessError) return err.toJSON();
  if (err instanceof Error) {
    // The runtime-unavailable error lives in `python-client` (which imports this
    // module), so it is detected by its stable name/code rather than `instanceof`
    // to avoid a circular import. It must keep its own code so the UI can render
    // an honest "runtime unavailable" state instead of a generic error.
    if ((err as { code?: unknown }).code === "RUNTIME_STARTING") {
      return { code: "RUNTIME_STARTING", message: redactSecrets(err.message || "Runtime starting") };
    }
    if (err.name === "HarnessRuntimeUnavailable" || (err as { code?: unknown }).code === "RUNTIME_UNAVAILABLE") {
      return { code: "RUNTIME_UNAVAILABLE", message: redactSecrets(err.message || "Agent runtime unavailable") };
    }
    if (err.name === "AbortError" || err.name === "TimeoutError") {
      return { code: "PROVIDER_TIMEOUT", message: redactSecrets(err.message || "Request timed out") };
    }
    return { code: fallbackCode, message: redactSecrets(err.message || "Unknown error") };
  }
  return { code: fallbackCode, message: redactSecrets(String(err)) };
}

export function isHarnessError(err: unknown): err is HarnessError {
  return err instanceof HarnessError;
}
