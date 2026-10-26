import { AGENT_NAMES, agentLabel, workdir, type AgentKind, type AgentSession, type SessionStatus } from "../api";
import { timeAgo } from "../format";
import { StatusBadge } from "./StatusBadge";
import { summarize, truncate } from "./Transcript";

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
      <span className="muted small">{timeAgo(lastActive(session))}</span>
    </button>
  );
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
