export const API_BASE = import.meta.env.VITE_SCBIO_API ?? "/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export interface Project {
  id: string;
  name?: string;
  description?: string;
}

export interface GraphPayload {
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
  artifact_versions: Array<Record<string, unknown>>;
  as_of_event_id?: number | null;
  as_of_ts?: string | null;
}

export interface TimelineEvent {
  id: number;
  ts: string;
  type: string;
  summary: string;
  payload: Record<string, unknown>;
}

export const api = {
  health: () => req<{ ok: boolean; hermes: { ok: boolean; detail: string } }>("/health"),
  listProjects: () => req<{ projects: Project[] }>("/projects"),
  createProject: (name: string) =>
    req<Project>("/projects", { method: "POST", body: JSON.stringify({ name }) }),
  plan: (id: string, intent = "") =>
    req(`/projects/${id}/plan`, {
      method: "POST",
      body: JSON.stringify({ intent, ping_hermes: false }),
    }),
  runAll: (id: string) =>
    req(`/projects/${id}/run`, { method: "POST", body: JSON.stringify({}) }),
  runStage: (id: string, stage: string, mode = "full") =>
    req(`/projects/${id}/run/${stage}`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
  graph: (id: string, asOf?: number) =>
    req<GraphPayload>(`/projects/${id}/graph${asOf != null ? `?as_of=${asOf}` : ""}`),
  timeline: (id: string) => req<{ events: TimelineEvent[] }>(`/projects/${id}/timeline`),
  artifact: (id: string, logicalId: string) =>
    req(`/projects/${id}/artifacts/${encodeURIComponent(logicalId)}`),
  aiEdit: (
    id: string,
    body: {
      logical_id: string;
      instruction: string;
      scope: string;
      impact_tier?: string;
      context_text?: string;
    },
  ) =>
    req(`/projects/${id}/edits/ai`, { method: "POST", body: JSON.stringify(body) }),
  patchParams: (
    id: string,
    stage: string,
    body: {
      compute_params?: Record<string, unknown>;
      presentation_params?: Record<string, unknown>;
      rerun?: boolean;
    },
  ) =>
    req(`/projects/${id}/stages/${stage}/params`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
};
