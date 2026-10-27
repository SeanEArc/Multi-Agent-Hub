import {
  AGENT_NAMES,
  agentLabel,
  workdir,
  type AgentKind,
  type AgentSession,
  type SessionEvent,
  type SessionStatus,
} from "../api";
import { timeAgo } from "../format";
import { planCounts, type PlanItem } from "../progress";
import { StatusBadge } from "./StatusBadge";
import { PLAN_LABELS, summarize, truncate } from "./Transcript";

// A card lists at most this many plan steps, around the current one.
const CARD_PLAN_STEPS = 5;

interface Props {
  sessions: AgentSession[];
  onSelect: (id: string) => void;
  onNew: () => void;
}

const GROUPS: { title: string; statuses: SessionStatus[] }[] = [
  { title: "Needs you", statuses: ["waiting_for_input", "failed"] },
  { title: "Working", statuses: ["working", "handing_off"] },
  { title: "Idle", statuses: ["ready", "completed"] },
];

/** Every session at a glance, the ones waiting on the user first. */
export function Dashboard({ sessions, onSelect, onNew }: Props) {
  if (sessions.length === 0) {
    return (
      <div className="empty-state">
        <h2>No sessions yet</h2>
        <p className="muted">Start a session to put an agent to work on a project.</p>
        <button className="button primary" onClick={onNew}>
          + New session
        </button>
      </div>
    );
  }

  const groups = GROUPS.map((group) => ({
    ...group,
    sessions: sessions
      .filter((s) => group.statuses.includes(s.status))
      .sort((a, b) => lastActive(b).localeCompare(lastActive(a))),
  })).filter((group) => group.sessions.length > 0);

  return (
    <section className="dashboard">
      <header className="dashboard-header">
        <h1>Sessions</h1>
        <span className="muted small">
          {groups.map((group) => `${group.sessions.length} ${group.title.toLowerCase()}`).join(" · ")}
        </span>
      </header>
      {groups.map((group) => (
        <section key={group.title} className="dashboard-group" aria-label={group.title}>
          <h2>{group.title}</h2>
          <div className="dashboard-grid">
            {group.sessions.map((session) => (
              <SessionCard key={session.id} session={session} onSelect={() => onSelect(session.id)} />
            ))}
          </div>
        </section>
      ))}
    </section>
  );
}

function SessionCard({ session, onSelect }: { session: AgentSession; onSelect: () => void }) {
  const worktree = session.worktree;
  return (
    <button className={`session-card status-${session.status}`} onClick={onSelect}>
      <span className="session-card-top">
        <span className="session-card-title">{session.title}</span>
        <StatusBadge status={session.status} />
      </span>
      <span className="session-card-meta">
        <span className="chip">{agentLabel(session)}</span>
        {worktree && (
          <span className={`chip mono${worktree.status === "removed" ? " removed" : ""}`}>⑂ {worktree.branch}</span>
        )}
        <span className="path">{folderName(session.project_path)}</span>
      </span>
      <span className="session-card-activity">{activity(session)}</span>
      {session.plan?.length ? <CardPlan plan={session.plan} /> : null}
      {session.running_subagents.length > 0 && <CardSubagents session={session} />}
      <span className="muted small">{timeAgo(lastActive(session))}</span>
    </button>
  );
}

/** The current agent's plan: progress, then a window of steps that keeps the current one in view. */
function CardPlan({ plan }: { plan: PlanItem[] }) {
  const { done, total } = planCounts(plan);
  const current = plan.findIndex((item) => item.status !== "completed");
  // Start one step before the current one, so the last finished step shows too.
  const start = Math.max(0, Math.min((current === -1 ? total : current) - 1, total - CARD_PLAN_STEPS));
  const shown = plan.slice(start, start + CARD_PLAN_STEPS);
  const hidden = total - shown.length;
  return (
    <span className="card-plan">
      <span className="plan-heading">
        <strong>Plan</strong>
        <span className="muted small">
          {done} of {total} done
        </span>
        <span
          className="plan-bar"
          role="progressbar"
          aria-label="Plan progress"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={done}
        >
          <span style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
        </span>
      </span>
      {/* Spans, not a list: the card is a button, which can't hold one. */}
      <span className="card-plan-steps">
        {shown.map((item, i) => (
          <span key={start + i} className={`plan-item ${item.status}`}>
            <span className="plan-mark" role="img" aria-label={PLAN_LABELS[item.status] ?? item.status} />
            <span className="card-plan-text">{item.text}</span>
          </span>
        ))}
      </span>
      {hidden > 0 && (
        <span className="muted small">
          {hidden} more {hidden === 1 ? "step" : "steps"}
        </span>
      )}
    </span>
  );
}

/** The subagents the session's agent is waiting on, each with its latest step. */
function CardSubagents({ session }: { session: AgentSession }) {
  const subagents = session.running_subagents;
  return (
    <span className="card-subagents">
      <span className="card-subagents-count">
        {subagents.length} {subagents.length === 1 ? "subagent" : "subagents"} running
      </span>
      {subagents.map((subagent) => (
        <span key={subagent.tool_use_id} className="card-subagent">
          <span className="card-subagent-name">
            <span className="chip">{subagent.kind}</span>
            <span className="card-subagent-description">{subagent.description || "Subagent"}</span>
          </span>
          {/* The count goes first, so a long step's ellipsis doesn't hide it. */}
          <span className="card-subagent-step muted">
            {subagent.last_step
              ? `${subagent.steps} ${subagent.steps === 1 ? "step" : "steps"} · ${stepLine(subagent.last_step, workdir(session))}`
              : "Starting…"}
          </span>
        </span>
      ))}
    </span>
  );
}

function stepLine(event: SessionEvent, projectPath: string): string {
  const p = event.payload;
  return event.type === "message" ? firstLine(p.text) : `${p.name} ${summarize(p.input ?? {}, projectPath)}`;
}

function lastActive(session: AgentSession): string {
  return session.last_event?.created_at ?? session.updated_at;
}

/** One line saying what the session did last. */
function activity(session: AgentSession): string {
  const event = session.last_event;
  if (!event) return "Not started yet";
  const p = event.payload;
  switch (event.type) {
    case "user_message":
      return `You: ${firstLine(p.text)}`;
    case "message":
      return firstLine(p.text);
    case "tool_use":
      return `${p.name} ${summarize(p.input ?? {}, workdir(session))}`;
    case "question":
      return `Asked: ${firstLine(p.text)}`;
    case "error":
      return p.stopped ? "Stopped" : `Error: ${firstLine(p.message || p.subtype || "something went wrong")}`;
    case "handoff":
      return `Handed off to ${AGENT_NAMES[p.to_agent as AgentKind] ?? p.to_agent}`;
    case "handoff_offer":
      return `Usage limit reached; can hand off to ${AGENT_NAMES[p.agent as AgentKind] ?? p.agent}`;
    case "limit_reached":
      return "Usage limit reached";
    default:
      return event.type;
  }
}

function firstLine(text: string | undefined): string {
  const line = (text ?? "").split("\n").find((l) => l.trim()) ?? "";
  return truncate(line.replace(/^#+\s*/, "").trim(), 160);
}

export function folderName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
}
