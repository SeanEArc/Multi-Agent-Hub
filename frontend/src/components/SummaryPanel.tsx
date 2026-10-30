import { useEffect, useState } from "react";
import { MarkdownText } from "./MarkdownText";
import { api, type AgentSession, type SessionEvent, type SessionSummary } from "../api";
import { formatTime } from "../format";
import { formatTokens, type StintHistory, type TurnOutcome } from "../history";
import { planCounts, type PlanItem } from "../progress";
import { agentName, truncate } from "./Transcript";

interface Props {
  session: AgentSession;
  events: SessionEvent[];
  history: StintHistory[];
  // Called after a new summary is written, so the dashboard shows it.
  onWritten: () => void;
}

const LAST_TURN: Record<TurnOutcome, string> = {
  done: "finished",
  question: "asked you a question",
  limit: "stopped at a usage limit",
  error: "ended with an error",
  stopped: "was stopped",
  running: "is running",
  cut_off: "was cut off",
};
// While another view started a summary, check back this often.
const POLL_MS = 3000;

/**
 * What the session is about and where it stands: the hub's own summary, built live from the events
 * and git at no usage cost, and the latest summary an agent wrote on request (Re-summarize).
 */
export function SummaryPanel({ session, events, history, onWritten }: Props) {
  const [info, setInfo] = useState<SessionSummary | null>(null);
  const [writing, setWriting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const turns = history.flatMap((h) => h.turns);
  const lastTurn = turns.at(-1);
  // Refresh git and the written summary whenever a turn starts or ends.
  const turnKey = `${turns.length}:${lastTurn?.outcome ?? ""}`;
  useEffect(() => {
    let live = true;
    api.getSummary(session.id).then((next) => live && setInfo(next)).catch(() => {});
    return () => {
      live = false;
    };
  }, [session.id, turnKey]);

  // A summary started before this view opened: wait for it.
  useEffect(() => {
    if (!info?.summarizing || writing) return;
    const timer = setTimeout(() => {
      api.getSummary(session.id).then((next) => {
        setInfo(next);
        if (!next.summarizing) onWritten();
      }).catch(() => {});
    }, POLL_MS);
    return () => clearTimeout(timer);
  }, [info, writing, session.id]);

  async function write() {
    setWriting(true);
    setError(null);
    try {
      const summary = await api.writeSummary(session.id);
      setInfo((current) => ({ git: current?.git ?? null, summary, summarizing: false }));
      onWritten();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setWriting(false);
    }
  }

  const userMessages = events.filter((e) => e.type === "user_message").map((e) => e.payload.text as string);
  const task = userMessages[0];
  const latest = userMessages.length > 1 ? userMessages.at(-1) : undefined;
  const plan = currentPlan(events, session.stints.at(-1)?.id);
  const step = plan?.find((item) => item.status !== "completed");
  const counts = plan ? planCounts(plan) : null;
  const lastMessage = lastTurn ? lastAgentMessage(events, lastTurn.start.id) : null;
  const files = new Set(turns.flatMap((t) => t.files)).size;
  const git = info?.git;
  const uncommitted = git ? git.status.split("\n").filter((line) => line.trim()).length : 0;
  const diffTotal = git?.diff_stat.trim().split("\n").at(-1)?.trim();
  const summary = info?.summary;
  const busy = writing || !!info?.summarizing;

  return (
    <section className="summary-panel" aria-label="Summary">
      <header className="summary-header">
        <h2>Summary</h2>
        <button
          className="button"
          onClick={() => void write()}
          disabled={busy}
          title={`${agentName(session.agent)} writes a short summary in a separate call (uses some of its usage; the next agent in the chain writes it if ${agentName(session.agent)} is out)`}
        >
          {busy ? "Writing…" : summary ? "Re-summarize" : "Summarize"}
        </button>
      </header>

      {task ? (
        <dl className="summary-facts">
          <dt>Task</dt>
          <dd className="pre-wrap">{truncate(task, 400)}</dd>
          {latest && (
            <>
              <dt>Latest request</dt>
              <dd className="pre-wrap">{truncate(latest, 300)}</dd>
            </>
          )}
          {counts && (
            <>
              <dt>Plan</dt>
              <dd>
                {counts.done} of {counts.total} done
                {step ? ` · now: ${step.text}` : " · all steps done"}
              </dd>
            </>
          )}
          {lastTurn && (
            <>
              <dt>Last turn</dt>
              <dd>
                {agentName(lastTurn.end?.agent ?? lastTurn.start.agent)} {LAST_TURN[lastTurn.outcome]}
                {lastTurn.endedAt && lastTurn.outcome !== "running" ? ` · ${formatTime(lastTurn.endedAt)}` : ""}
                {lastMessage && <span className="summary-quote">{truncate(lastMessage, 400)}</span>}
              </dd>
            </>
          )}
          {(files > 0 || diffTotal || uncommitted > 0) && (
            <>
              <dt>Changes</dt>
              <dd>
                {[
                  files ? `${files} file${files === 1 ? "" : "s"} edited by the agents` : null,
                  diffTotal ? `branch: ${diffTotal}` : null,
                  uncommitted ? `${uncommitted} uncommitted` : null,
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </dd>
            </>
          )}
        </dl>
      ) : (
        <p className="muted small">Nothing to summarize yet: the session hasn't started.</p>
      )}

      {summary && (
        <div className="summary-written">
          <div className="summary-meta muted small">
            Written by {agentName(summary.agent)}
            {summary.model ? ` (${summary.model})` : ""} · {formatTime(summary.created_at)}
            {summary.tokens !== null ? ` · ${formatTokens(summary.tokens)} tokens` : ""}
            {summary.fallback_from && ` · ${agentName(summary.fallback_from)} was out of usage or unavailable`}
          </div>
          {summary.outdated && (
            <div className="summary-outdated small">
              Outdated: the session has moved on since this was written. Re-summarize for a current one.
            </div>
          )}
          <div className="assistant-message summary-text">
            <MarkdownText>{summary.text}</MarkdownText>
          </div>
        </div>
      )}
      {error && <div className="summary-error small">{error}</div>}
    </section>
  );
}

function currentPlan(events: SessionEvent[], stintId: number | undefined): PlanItem[] | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.type === "plan" && e.stint_id === stintId && !e.payload.parent_tool_use_id) return e.payload.items ?? null;
  }
  return null;
}

/** The agent's last message in the turn that started at `sinceId`. */
function lastAgentMessage(events: SessionEvent[], sinceId: number): string | null {
  for (let i = events.length - 1; i >= 0 && events[i].id > sinceId; i--) {
    const e = events[i];
    if (e.type === "message" && !e.payload.parent_tool_use_id && e.payload.text) return e.payload.text;
  }
  return null;
}
