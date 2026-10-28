// A session's history, derived from its events: each agent's stint and the turns it ran.
import type { SessionEvent, Stint } from "./api";
import { editedPaths, isSubagent, planCounts } from "./progress";

export type TurnOutcome = "done" | "question" | "limit" | "error" | "stopped" | "running" | "cut_off";

export interface Turn {
  // The user message or handoff that started it; the transcript can scroll to it.
  start: SessionEvent;
  // The user's message, or null when the turn started on a handoff brief.
  prompt: string | null;
  // A note the user added to the handoff that started this turn.
  note: string | null;
  outcome: TurnOutcome;
  // The event that ended it (completed, question, limit_reached or error).
  end: SessionEvent | null;
  endedAt: string | null;
  errorMessage: string | null;
  toolCalls: number;
  subagents: number;
  files: string[];
  tokens: number;
  costUsd: number | null;
  // The plan as it stood when the turn ended.
  plan: { done: number; total: number } | null;
}

export interface StintHistory {
  stint: Stint;
  // The handoff event that started it; null for the first stint.
  handoff: SessionEvent | null;
  turns: Turn[];
}

export interface HistoryTotals {
  turns: number;
  toolCalls: number;
  files: number;
  tokens: number;
  costUsd: number | null;
}

const TURN_ENDS: Record<string, TurnOutcome> = {
  completed: "done",
  question: "question",
  limit_reached: "limit",
  error: "error",
};

/**
 * Groups the session's events by stint and splits each stint into turns. A turn starts with a
 * user message or a handoff (whose note, if any, follows as a user message) and ends with
 * completed, question, limit_reached or error. A turn with no end is still running, or was cut
 * off if something came after it.
 */
export function deriveHistory(events: SessionEvent[], stints: Stint[], running: boolean): StintHistory[] {
  const byId = new Map<number, StintHistory>(stints.map((stint) => [stint.id, { stint, handoff: null, turns: [] }]));
  const tokens = turnTokens(events);
  // Events saved before stints existed have no stint id; they belong to the first.
  const fallback = stints[0]?.id;
  let turn: Turn | null = null;
  let open = false;
  let previous: SessionEvent | null = null;

  for (const event of events) {
    const before = previous;
    previous = event;
    const history = byId.get(event.stint_id ?? fallback ?? -1);
    if (!history) continue;
    const p = event.payload;

    if (event.type === "handoff") {
      if (turn && open) turn.outcome = "cut_off";
      history.handoff = event;
      turn = newTurn(event, null);
      history.turns.push(turn);
      open = true;
      continue;
    }
    if (event.type === "user_message") {
      // The handoff note arrives right after the handoff, before the new agent's first event.
      if (turn && open && turn.start.type === "handoff" && turn.start === before) {
        turn.note = p.text;
        continue;
      }
      if (turn && open) turn.outcome = "cut_off";
      turn = newTurn(event, p.text);
      history.turns.push(turn);
      open = true;
      continue;
    }
    if (!turn || !open) continue;

    if (event.type === "tool_use" && !p.parent_tool_use_id) {
      turn.toolCalls += 1;
      if (isSubagent(event)) turn.subagents += 1;
    }
    if (event.type === "tool_use") {
      for (const path of editedPaths(p.name, p.input ?? {})) if (!turn.files.includes(path)) turn.files.push(path);
    }
    if (event.type === "plan" && !p.parent_tool_use_id) {
      const { done, total } = planCounts(p.items ?? []);
      turn.plan = { done, total };
    }
    if (event.type === "usage") {
      turn.tokens = tokens.get(event.id) ?? 0;
      if (typeof p.cost_usd === "number") turn.costUsd = p.cost_usd;
    }
    const outcome = TURN_ENDS[event.type];
    if (outcome) {
      turn.outcome = event.type === "error" && p.stopped ? "stopped" : outcome;
      turn.end = event;
      turn.endedAt = event.created_at;
      if (event.type === "error" && !p.stopped) turn.errorMessage = p.message || p.subtype || null;
      open = false;
    }
  }

  // A turn left open is still running only if it is the last one and the session is working.
  const result = [...byId.values()];
  for (const history of result) {
    for (const t of history.turns) {
      if (t.outcome === "running" && !(running && t === turn)) t.outcome = "cut_off";
    }
  }
  return result;
}

export function historyTotals(history: StintHistory[]): HistoryTotals {
  const turns = history.flatMap((h) => h.turns);
  const costs = turns.map((t) => t.costUsd).filter((c): c is number => c !== null);
  return {
    turns: turns.length,
    toolCalls: turns.reduce((sum, t) => sum + t.toolCalls, 0),
    files: new Set(turns.flatMap((t) => t.files)).size,
    tokens: turns.reduce((sum, t) => sum + t.tokens, 0),
    costUsd: costs.length ? costs.reduce((sum, c) => sum + c, 0) : null,
  };
}

function newTurn(start: SessionEvent, prompt: string | null): Turn {
  return {
    start,
    prompt,
    note: null,
    outcome: "running",
    end: null,
    endedAt: null,
    errorMessage: null,
    toolCalls: 0,
    subagents: 0,
    files: [],
    tokens: 0,
    costUsd: null,
    plan: null,
  };
}

/**
 * Each usage event's tokens for its own turn. Codex reports the thread's running total, so a
 * turn's own tokens are the growth since the last report; a handoff starts a new thread.
 */
export function turnTokens(events: SessionEvent[]): Map<number, number> {
  const counts = new Map<number, number>();
  let threadTokens = 0;
  for (const event of events) {
    if (event.type === "handoff") threadTokens = 0;
    if (event.type !== "usage") continue;
    const total = tokenTotal(event.payload.usage);
    // Events saved before the `cumulative` flag existed are Codex totals too.
    if (event.payload.cumulative ?? event.agent === "codex") {
      // A smaller total means a new thread started (e.g. the first turn failed before Codex saved it).
      counts.set(event.id, total >= threadTokens ? total - threadTokens : total);
      threadTokens = total;
    } else {
      counts.set(event.id, total);
    }
  }
  return counts;
}

/**
 * Every token the model processed. Codex's input_tokens already includes cached input;
 * Claude reports cache writes and reads separately from input_tokens, so they are added.
 */
function tokenTotal(usage: Record<string, number> | null | undefined): number {
  return (
    (usage?.input_tokens ?? 0) +
    (usage?.cache_creation_input_tokens ?? 0) +
    (usage?.cache_read_input_tokens ?? 0) +
    (usage?.output_tokens ?? 0)
  );
}

export function formatTokens(count: number): string {
  return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
}
