// ── Agent presets — browser client ───────────────────────────────────────────
// Thin wrappers over /api/agent-presets (which proxies Python /agents/*). No
// preset logic lives here; the runtime validates and versions every write.

export interface AgentPresetEffect {
  name: string;
  description: string;
  permission: 'read' | 'write' | 'external_action';
  risk: 'none' | 'low' | 'medium' | 'high';
}

export interface AgentPresetSummary {
  id: string;
  name: string;
  description: string;
  version: number;
  maxSteps: number;
  toolCount: number;
  effects: string[];
  archived: boolean;
  createdAt: string;
  updatedAt: string;
}

export interface AgentPreset extends AgentPresetSummary {
  systemPrompt: string;
  tools: string[];
  allowMcpTools: boolean;
  allowPluginTools: boolean;
  allowEffectful: boolean;
  contentHash: string;
}

export interface AgentPresetInput {
  name: string;
  description?: string;
  systemPrompt: string;
  tools: string[];
  maxSteps?: number;
  allowMcpTools?: boolean;
  allowPluginTools?: boolean;
  allowEffectful?: boolean;
}

export class AgentPresetApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = 'AgentPresetApiError';
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) {
    throw new AgentPresetApiError(
      typeof data.error === 'string' ? data.error : `Request failed (${res.status})`,
      res.status,
    );
  }
  return data as T;
}

export async function listAgentPresets(includeArchived = false): Promise<AgentPresetSummary[]> {
  const data = await request<{ presets?: AgentPresetSummary[] }>(
    `/api/agent-presets/presets${includeArchived ? '?includeArchived=true' : ''}`,
  );
  return data.presets ?? [];
}

export async function getAgentPreset(id: string, version?: number): Promise<AgentPreset> {
  const qs = version ? `?version=${version}` : '';
  const data = await request<{ preset: AgentPreset }>(`/api/agent-presets/presets/${encodeURIComponent(id)}${qs}`);
  return data.preset;
}

export async function createAgentPreset(input: AgentPresetInput): Promise<AgentPreset> {
  const data = await request<{ preset: AgentPreset }>('/api/agent-presets/presets', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return data.preset;
}

export async function updateAgentPreset(id: string, input: AgentPresetInput): Promise<AgentPreset> {
  const data = await request<{ preset: AgentPreset }>(`/api/agent-presets/presets/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  });
  return data.preset;
}

export async function archiveAgentPreset(id: string): Promise<void> {
  await request(`/api/agent-presets/presets/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function listAgentPresetVersions(id: string): Promise<Array<{ version: number; contentHash: string; createdAt: string }>> {
  const data = await request<{ versions?: Array<{ version: number; contentHash: string; createdAt: string }> }>(
    `/api/agent-presets/presets/${encodeURIComponent(id)}/versions`,
  );
  return data.versions ?? [];
}

export async function listBuiltInAgents(): Promise<Array<{ id: string; name: string }>> {
  const data = await request<{ agents?: Array<{ id: string; name: string }> }>('/api/agent-presets/builtins');
  return data.agents ?? [];
}

export async function listAgentTools(): Promise<AgentPresetEffect[]> {
  const data = await request<{ tools?: AgentPresetEffect[] }>('/api/agent-presets/tools');
  return data.tools ?? [];
}

/** The built-ins the runtime always has, shown alongside custom presets. */
export const BUILT_IN_AGENTS = [
  { id: 'eclipse-agent', name: 'Eclipse Agent', description: 'General finance and research agent.' },
  { id: 'code-navigator', name: 'Repository Navigator', description: 'Closed read/write repository toolset.' },
  { id: 'code-agent', name: 'Eclipse Code Agent', description: 'Coding agent with execution and verification tools.' },
  { id: 'desktop-agent', name: 'Desktop Agent', description: 'Drives native apps on the real desktop: see, act, verify.' },
] as const;
