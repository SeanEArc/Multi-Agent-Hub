// Types and calls for the FastAPI backend (see backend/app/api/sessions.py).

export type SessionStatus =
  | "ready"
  | "working"
  | "handing_off"
  | "waiting_for_input"
  | "completed"
  | "failed";

export type AgentKind = "claude" | "codex" | "ollama";

export interface AgentSession {
  id: string;
  title: string;
  project_path: string;
  agent: AgentKind;
  model: string | null;
  status: SessionStatus;
  native_session_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface SessionEvent {
  id: number;
  session_id: string;
  agent: AgentKind;
  type: string;
  payload: Record<string, any>;
  created_at: string;
}

export interface NewSession {
  title: string;
  project_path: string;
  agent: AgentKind;
  model: string | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: init?.body ? { "Content-Type": "application/json" } : undefined,
  });
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response));
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

async function errorMessage(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    // FastAPI validation errors: [{loc: [..., "field"], msg: "..."}]
    if (Array.isArray(body.detail)) {
      return body.detail.map((d: any) => `${d.loc?.at(-1) ?? "input"}: ${d.msg}`).join("; ");
    }
  } catch {
    // not JSON
  }
  return `Request failed (${response.status})`;
}

export const api = {
  listSessions: () => request<AgentSession[]>("/sessions"),
  createSession: (body: NewSession) =>
    request<AgentSession>("/sessions", { method: "POST", body: JSON.stringify(body) }),
  deleteSession: (id: string) => request<void>(`/sessions/${id}`, { method: "DELETE" }),
  sendMessage: (id: string, text: string) =>
    request<AgentSession>(`/sessions/${id}/messages`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  stop: (id: string) => request<AgentSession>(`/sessions/${id}/stop`, { method: "POST" }),
  streamUrl: (id: string) => `/api/sessions/${id}/stream`,
};
