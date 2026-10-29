import { useState } from "react";
import type { AgentSession, SessionEvent, SessionStatus } from "../api";
import { formatDuration, formatTime } from "../format";
import { deriveHistory, formatTokens, historyTotals, type StintHistory, type Turn, type TurnOutcome } from "../history";
import { SummaryPanel } from "./SummaryPanel";
import { REASONS, agentName, relativeTo, truncate, useBrief } from "./Transcript";

interface Props {
  session: AgentSession;
  events: SessionEvent[];
  status: SessionStatus;
  projectPath: string;
  // Switches to the transcript, scrolled to the event that started the turn.
  onShowInTranscript: (eventId: number) => void;
  // A new summary was written.
  onChanged: () => void;
}

const OUTCOMES: Record<TurnOutcome, string> = {
  done: "Done",
  question: "Asked you",
  limit: "Usage limit",
  error: "Error",
  stopped: "Stopped",
  running: "Running",
  cut_off: "Cut off",
};

/** The session's summary, then its stints, handoffs and turns, newest stint last, for reviewing after the fact. */
export function HistoryView({ session, events, status, projectPath, onShowInTranscript, onChanged }: Props) {
  const history = deriveHistory(events, session.stints, status === "working");
  const totals = historyTotals(history);
  const started = session.stints[0]?.started_at ?? session.created_at;
  const last = events.at(-1)?.created_at ?? session.updated_at;

  return (
    <div className="history">
      <SummaryPanel session={session} events={events} history={history} onWritten={onChanged} />
      <div className="history-totals">
        <Stat label="Agents" value={String(history.length)} />
        <Stat label="Turns" value={String(totals.turns)} />
        <Stat label="Tool calls" value={String(totals.toolCalls)} />
        <Stat label="Files edited" value={String(totals.files)} />
        <Stat label="Tokens" value={formatTokens(totals.tokens)} />
        {totals.costUsd !== null && (
          <Stat
            label="API price"
            value={`$${totals.costUsd.toFixed(2)}`}
            title="What Claude's turns would cost at API prices. Subscription use is not billed per turn; Codex reports no price."
          />
        )}
        <Stat
          label="Span"
          value={formatDuration(started, last)}
          title={`${formatTime(started)} to ${formatTime(last)}`}
        />
      </div>

      {history.map((item, index) => (
        <StintCard
          key={item.stint.id}
          sessionId={session.id}
          item={item}
          number={index + 1}
          current={index === history.length - 1}
          projectPath={projectPath}
          onShowInTranscript={onShowInTranscript}
        />
      ))}
    </div>
  );
}

function Stat({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="history-stat" title={title}>
      <span className="history-stat-value">{value}</span>
      <span className="history-stat-label">{label}</span>
    </div>
  );
}

interface StintCardProps {
  sessionId: string;
  item: StintHistory;
  number: number;
  current: boolean;
  projectPath: string;
  onShowInTranscript: (eventId: number) => void;
}

function StintCard({ sessionId, item, number, current, projectPath, onShowInTranscript }: StintCardProps) {
  const { stint, handoff, turns } = item;
  const brief = useBrief(sessionId, stint.id);
  const model = handoff?.payload.model ?? stint.model;
  const reason = stint.reason === "new" ? "started the session" : (REASONS[stint.reason] ?? stint.reason);
  const from = handoff ? ` from ${agentName(handoff.payload.from_agent)}` : "";

  return (
    <section className="history-stint">
      <header className="history-stint-header">
        <div>
          <h2>
            <span className="history-stint-number">{number}</span>
            {agentName(stint.agent)}
            {model && <span className="chip">{model}</span>}
            {current && <span className="chip">current</span>}
          </h2>
          <div className="muted small">
            {stint.reason === "new" ? reason : `Took over${from} · ${reason}`} · {formatTime(stint.started_at)}
            {stint.ended_at &&
              ` to ${formatTime(stint.ended_at)} (${formatDuration(stint.started_at, stint.ended_at)})`}
          </div>
        </div>
        {stint.reason !== "new" && brief.button}
      </header>
      {brief.panel}

      {turns.length === 0 ? (
        <p className="muted small">No turns yet.</p>
      ) : (
        <ol className="history-turns">
          {turns.map((turn) => (
            <TurnRow key={turn.start.id} turn={turn} projectPath={projectPath} onShow={onShowInTranscript} />
          ))}
        </ol>
      )}
    </section>
  );
}

function TurnRow({
  turn,
  projectPath,
  onShow,
}: {
  turn: Turn;
  projectPath: string;
  onShow: (eventId: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const text =
    turn.prompt ?? (turn.note ? `Handoff brief, with your note: ${turn.note}` : "Continued from the handoff brief");
  const stats = [
    turn.endedAt ? formatDuration(turn.start.created_at, turn.endedAt) : null,
    turn.toolCalls ? `${turn.toolCalls} tool call${turn.toolCalls === 1 ? "" : "s"}` : null,
    turn.subagents ? `${turn.subagents} subagent${turn.subagents === 1 ? "" : "s"}` : null,
    turn.files.length ? `${turn.files.length} file${turn.files.length === 1 ? "" : "s"} edited` : null,
    turn.plan ? `plan ${turn.plan.done} of ${turn.plan.total}` : null,
    turn.costUsd !== null ? `$${turn.costUsd.toFixed(4)}` : null,
    turn.tokens ? `${formatTokens(turn.tokens)} tokens` : null,
  ].filter(Boolean);

  return (
    <li className="history-turn">
      <div className="history-turn-line">
        <span className={`history-outcome outcome-${turn.outcome}`}>{OUTCOMES[turn.outcome]}</span>
        <span className="history-time">{formatTime(turn.start.created_at)}</span>
        <button
          className="history-prompt"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
          title={open ? "Hide details" : "Show details"}
        >
          {truncate(text.replace(/\s+/g, " "), 160)}
        </button>
        <button className="button subtle" onClick={() => onShow(turn.start.id)}>
          Show in transcript
        </button>
      </div>
      {stats.length > 0 && <div className="history-turn-stats muted small">{stats.join(" · ")}</div>}
      {turn.errorMessage && <div className="history-turn-error small">{truncate(turn.errorMessage, 300)}</div>}
      {open && (
        <div className="history-turn-detail">
          <div className="tool-label">{turn.prompt ? "Your message" : "Started from"}</div>
          <p className="pre-wrap">{turn.prompt ?? text}</p>
          {turn.files.length > 0 && (
            <>
              <div className="tool-label">Files edited</div>
              <ul className="history-files">
                {turn.files.map((path) => (
                  <li key={path} className="path">
                    {relativeTo(projectPath, path)}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </li>
  );
}
