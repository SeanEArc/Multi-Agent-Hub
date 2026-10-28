// What a session is working on, derived from its events: the agent's plan, its subagents and the files it changed.
import type { SessionEvent } from "./api";

export type PlanStatus = "pending" | "in_progress" | "completed";

export interface PlanItem {
  text: string;
  status: PlanStatus;
}

export interface Subagent {
  event: SessionEvent;
  kind: string;
  description: string;
  // The subagent's own messages and tool calls.
  steps: SessionEvent[];
  state: "running" | "done" | "failed" | "stopped";
}

export interface FileTouched {
  path: string;
  edits: number;
}

/** Each tool call's result, and the calls refused for lack of approval, by tool call id. */
export interface ToolOutcomes {
  results: Map<string, SessionEvent>;
  denied: Set<string>;
}

export function toolOutcomes(events: SessionEvent[]): ToolOutcomes {
  const results = new Map<string, SessionEvent>();
  const denied = new Set<string>();
  for (const event of events) {
    if (event.type === "tool_result") results.set(event.payload.tool_use_id, event);
    if (event.type === "permission_denied") denied.add(event.payload.tool_use_id);
  }
  return { results, denied };
}

export interface Progress {
  plan: PlanItem[] | null;
  // Subagents started since the user's last message (or the last handoff).
  subagents: Subagent[];
  // Every file the session's agents edited, most recent first.
  files: FileTouched[];
}

// Claude Code's subagent tool was renamed from Task to Agent; both are recognized.
const SUBAGENT_TOOLS = ["Task", "Agent"];
const EDIT_TOOLS = ["Edit", "Write", "MultiEdit", "NotebookEdit"];

export function isSubagent(event: SessionEvent): boolean {
  return event.type === "tool_use" && !event.payload.parent_tool_use_id && SUBAGENT_TOOLS.includes(event.payload.name);
}

/** Events that belong to a subagent, by the id of the tool call that started it. */
export function stepsByParent(events: SessionEvent[]): Map<string, SessionEvent[]> {
  const steps = new Map<string, SessionEvent[]>();
  for (const event of events) {
    const parent = event.payload.parent_tool_use_id;
    if (!parent || (event.type !== "tool_use" && event.type !== "message")) continue;
    steps.set(parent, [...(steps.get(parent) ?? []), event]);
  }
  return steps;
}

export function deriveProgress(
  events: SessionEvent[],
  outcomes: ToolOutcomes,
  currentStintId: number | undefined,
  running: boolean,
): Progress {
  let plan: PlanItem[] | null = null;
  let turnStart = 0;
  const files = new Map<string, FileTouched>();

  events.forEach((event, index) => {
    const p = event.payload;
    if (event.type === "plan" && event.stint_id === currentStintId) plan = p.items ?? [];
    if (event.type === "user_message" || event.type === "handoff") turnStart = index;
    if (event.type === "tool_use") {
      for (const path of editedPaths(p.name, p.input ?? {})) {
        const entry = files.get(path) ?? { path, edits: 0 };
        entry.edits += 1;
        // Re-inserting moves the file to the end, so the most recent edit is last.
        files.delete(path);
        files.set(path, entry);
      }
    }
  });

  const steps = stepsByParent(events);
  const subagents = events.slice(turnStart).filter(isSubagent).map((event): Subagent => {
    const result = outcomes.results.get(event.payload.tool_use_id);
    return {
      event,
      kind: event.payload.input?.subagent_type ?? "Subagent",
      description: event.payload.input?.description ?? "",
      steps: steps.get(event.payload.tool_use_id) ?? [],
      state: result ? (result.payload.is_error ? "failed" : "done") : running ? "running" : "stopped",
    };
  });

  return { plan, subagents, files: [...files.values()].reverse() };
}

/** The files a tool call edited: an edit tool's path, or every file in a Codex patch. */
export function editedPaths(tool: string, input: Record<string, any>): string[] {
  // Codex reports one Edit per patch, listing every file it changed.
  if (Array.isArray(input.changes)) return input.changes.map((c: any) => c.path).filter(Boolean);
  if (!EDIT_TOOLS.includes(tool)) return [];
  const path = input.file_path ?? input.notebook_path;
  return typeof path === "string" ? [path] : [];
}

export function planCounts(plan: PlanItem[]): { done: number; total: number; current: PlanItem | undefined } {
  return {
    done: plan.filter((item) => item.status === "completed").length,
    total: plan.length,
    current: plan.find((item) => item.status === "in_progress"),
  };
}
