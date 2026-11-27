// Browser-safe client for the A2A teammate surface. It only reads/creates the
// durable messages the Python runtime already owns; no agent logic here.

export interface A2AAgentView {
  id: string;
  name?: string;
  title?: string;
  status?: string;
  capabilities?: string[];
  enabled?: boolean;
}

export interface A2AMessageView {
  id: string;
  senderAgentId?: string;
  recipientAgentId?: string | null;
  type: string;
  status?: string;
  payload?: Record<string, unknown>;
  /** Unix seconds (the A2A envelope's clock), not an ISO string. */
  timestamp?: number;
  runId?: string | null;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/a2a${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
  });
  const body = (await res.json().catch(() => ({}))) as T & { error?: string };
  if (!res.ok) throw new Error(body.error ?? `a2a request failed (${res.status})`);
  return body;
}

export async function listA2AAgents(): Promise<A2AAgentView[]> {
  const body = await call<{ agents: A2AAgentView[] }>("/agents");
  return body.agents ?? [];
}

export async function listA2AMessages(limit = 30): Promise<A2AMessageView[]> {
  const body = await call<{ messages: A2AMessageView[] }>(`/messages?limit=${limit}`);
  return body.messages ?? [];
}

export async function sendA2AMessage(input: {
  senderAgentId?: string;
  recipientAgentId: string;
  text: string;
}): Promise<A2AMessageView | null> {
  const body = await call<{ message?: A2AMessageView }>("/messages", {
    method: "POST",
    body: JSON.stringify({
      senderAgentId: input.senderAgentId ?? "eclipse-agent",
      recipientAgentId: input.recipientAgentId,
      type: "STATUS",
      payload: { text: input.text },
    }),
  });
  return body.message ?? null;
}
