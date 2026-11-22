"use client";

// ── LLM config change broadcast ──────────────────────────────────────────────
// The chat composer loads its provider/model options once, but Settings and
// onboarding are tabs inside the SAME app shell — connecting a provider there
// does not remount the composer. Without a signal, `useChatModel` kept the list
// fetched at boot (where only keyless locals like Ollama looked usable) and the
// operator's newly connected API's models never appeared.
//
// One window event keeps every mounted composer (the full chat and the floating
// dock are separate instances) in step with the saved vault config.

export const LLM_CONFIG_CHANGED_EVENT = "eclipse:llm-changed";

export interface LlmConfigChangedDetail {
  /**
   * A provider was just connected/activated. The composer should drop its
   * per-chat model choice and follow the newly saved provider instead of
   * staying pinned to an old local (Ollama/LM Studio) pick.
   */
  resetChoice?: boolean;
}

/** Tell every mounted composer that the saved LLM config changed. */
export function notifyLlmConfigChanged(detail: LlmConfigChangedDetail = {}): void {
  if (typeof window === "undefined") return;
  try {
    window.dispatchEvent(new CustomEvent<LlmConfigChangedDetail>(LLM_CONFIG_CHANGED_EVENT, { detail }));
  } catch {
    // A missing CustomEvent (very old WebView) only means no live refresh.
  }
}

/** Subscribe to config changes. Returns the unsubscribe function. */
export function onLlmConfigChanged(
  handler: (detail: LlmConfigChangedDetail) => void,
): () => void {
  if (typeof window === "undefined") return () => {};
  const listener = (event: Event) => {
    const detail = (event as CustomEvent<LlmConfigChangedDetail | undefined>).detail ?? {};
    handler(detail);
  };
  window.addEventListener(LLM_CONFIG_CHANGED_EVENT, listener);
  return () => window.removeEventListener(LLM_CONFIG_CHANGED_EVENT, listener);
}
