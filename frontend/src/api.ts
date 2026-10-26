// Types and calls for the FastAPI backend (see backend/app/api/sessions.py).

export type SessionStatus =
  | "ready"
  | "working"
  | "handing_off"
  | "waiting_for_input"
  | "completed"
  | "failed";

export type AgentKind = "claude" | "codex" | "ollama";

export const AGENT_NAMES: Record<AgentKind, string> = {
  claude: "Claude Code",
  codex: "Codex",
  ollama: "Local model",
};

const SHORT_NAMES: Record<AgentKind, string> = {
  claude: "Claude",
  codex: "Codex",
  ollama: "Local model",
};

/** "Claude (opus)" or "Codex (gpt-6.1-sol)": the current agent and the model it uses. */
export function agentLabel(session: AgentSession): string {
  return `${SHORT_NAMES[session.agent]} (${session.reported_model ?? session.model ?? "default"})`;
}

// Agents the backend can run; the local model arrives in phase 5.
export const AVAILABLE_AGENTS: AgentKind[] = ["claude", "codex"];

// Values each CLI accepts for --model; empty means the CLI's default.
// Codex slugs come from its model cache (codex-cli 0.164.0).
export const MODELS: Partial<Record<AgentKind, string[]>> = {
  claude: ["", "fable", "opus", "sonnet", "haiku"],
  codex: ["", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"],
};

export type StintReason = "new" | "limit" | "manual" | "delegation";
export type LimitPolicy = "auto" | "ask";

export interface ChainEntry {
  agent: AgentKind;
  model: string | null;
}

/** One agent's turn at a session, from when it took over until the next agent did. */
export interface Stint {
  id: number;
  seq: number;
  agent: AgentKind;
  model: string | null;
  native_session_id: string | null;
  reason: StintReason;
  parent_stint_id: number | null;
  started_at: string;
  ended_at: string | null;
}

export interface StintDetail extends Stint {
  brief: string | null;
}

/** A git worktree the hub made for a session: its own folder on its own branch. */
export interface Worktree {
  id: number;
  stint_id: number | null;
  repo_path: string;
  path: string;
  // The project folder relative to the repository's top level ("" at the top level).
  subdir: string;
  branch: string;
  base_commit: string;
  status: "active" | "removed";
  created_at: string;
  removed_at: string | null;
}

export interface AgentSession {
  id: string;
  title: string;
  project_path: string;
  // The current stint's agent, model and native session id.
  agent: AgentKind;
  model: string | null;
  status: SessionStatus;
  native_session_id: string | null;
  // The model the current agent said it used; names a default model (model is null).
  reported_model: string | null;
  // null means the defaults in HubSettings.
  chain: ChainEntry[] | null;
  limit_policy: LimitPolicy | null;
  stints: Stint[];
  // null when the session runs in the project folder itself.
  worktree: Worktree | null;
  // The latest activity in the main conversation (not usage reports or subagent steps).
  last_event: SessionEvent | null;
  created_at: string;
  updated_at: string;
}

/** Where the session's agents work: its worktree (plus the project's subfolder), or the project folder. */
export function workdir(session: AgentSession): string {
  const wt = session.worktree;
  if (!wt) return session.project_path;
  return wt.subdir ? `${wt.path}\\${wt.subdir.replaceAll("/", "\\")}` : wt.path;
}

/** An agent's latest plan usage windows, reported in any session. */
export interface AgentUsage {
  status: string | null;
  windows: Record<string, Record<string, any>>;
  plan_type: string | null;
  reported_at: string;
}

// Where the plan, subagents and changed files go: a card above the transcript, or a side panel.
export type ProgressLayout = "panel" | "split";

export interface HubSettings {
  default_chain: ChainEntry[];
  limit_policy: LimitPolicy;
  progress_layout: ProgressLayout;
}

export interface SessionEvent {
  id: number;
  session_id: string;
  stint_id: number | null;
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
  chain: ChainEntry[] | null;
  limit_policy: LimitPolicy | null;
  use_worktree: boolean;
}

export interface GitInfo {
  is_repo: boolean;
  root: string | null;
  branch: string | null;
  has_commits: boolean;
  // Uncommitted changes in the user's checkout (`git status --short`).
  changes: string[];
}

export interface FolderEntry {
  name: string;
  path: string;
}

export interface FolderListing {
  // null at the top level (home folder and drives).
  path: string | null;
  parent: string | null;
  folders: FolderEntry[];
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    // The response's `detail` when it is an object, such as {message, changes} from removing a worktree.
    readonly detail: Record<string, any> | null = null,
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
    const body = await response.json().catch(() => null);
    const detail = body?.detail && typeof body.detail === "object" && !Array.isArray(body.detail) ? body.detail : null;
    throw new ApiError(response.status, errorMessage(response, body), detail);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

function errorMessage(response: Response, body: any): string {
  const detail = body?.detail;
  if (typeof detail === "string") return detail;
  if (typeof detail?.message === "string") return detail.message;
  // FastAPI validation errors: [{loc: [..., "field"], msg: "..."}]
  if (Array.isArray(detail)) {
    return detail.map((d: any) => `${d.loc?.at(-1) ?? "input"}: ${d.msg}`).join("; ");
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
  handoff: (id: string, body: { agent: AgentKind; model: string | null; note?: string | null }) =>
    request<AgentSession>(`/sessions/${id}/handoff`, { method: "POST", body: JSON.stringify(body) }),
  removeWorktree: (id: string, body: { commit?: boolean; discard?: boolean } = {}) =>
    request<AgentSession>(`/sessions/${id}/worktree/remove`, { method: "POST", body: JSON.stringify(body) }),
  getStint: (id: string, stintId: number) => request<StintDetail>(`/sessions/${id}/stints/${stintId}`),
  getUsage: () => request<Partial<Record<AgentKind, AgentUsage>>>("/usage"),
  getSettings: () => request<HubSettings>("/settings"),
  putSettings: (body: HubSettings) =>
    request<HubSettings>("/settings", { method: "PUT", body: JSON.stringify(body) }),
  streamUrl: (id: string) => `/api/sessions/${id}/stream`,
  listFolders: (path: string | null, showHidden = false) => {
    const params = new URLSearchParams();
    if (path) params.set("path", path);
    if (showHidden) params.set("show_hidden", "true");
    return request<FolderListing>(`/folders?${params}`);
  },
  gitInfo: (path: string) => request<GitInfo>(`/folders/git?${new URLSearchParams({ path })}`),
};
