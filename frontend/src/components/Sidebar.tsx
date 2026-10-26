import { agentLabel, type AgentSession } from "../api";
import { folderName } from "./Dashboard";
import { StatusDot } from "./StatusBadge";

interface Props {
  sessions: AgentSession[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onNew: () => void;
  onSettings: () => void;
}

export function Sidebar({ sessions, selectedId, onSelect, onNew, onSettings }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span className="brand">Multi-Agent Hub</span>
      </div>
      <button className="button primary new-session" onClick={onNew}>
        + New session
      </button>
      <button
        className={`session-item dashboard-link${selectedId === null ? " selected" : ""}`}
        onClick={() => onSelect(null)}
      >
        All sessions
      </button>
      <nav className="session-list" aria-label="Sessions">
        {sessions.length === 0 && <p className="muted small">No sessions yet.</p>}
        {sessions.map((session) => (
          <button
            key={session.id}
            className={`session-item${session.id === selectedId ? " selected" : ""}`}
            onClick={() => onSelect(session.id)}
          >
            <StatusDot status={session.status} />
            <span className="session-item-text">
              <span className="session-item-title">{session.title}</span>
              <span className="session-item-meta">
                {agentLabel(session)} · {folderName(session.project_path)}
              </span>
            </span>
          </button>
        ))}
      </nav>
      <button className="button subtle sidebar-settings" onClick={onSettings}>
        Settings
      </button>
    </aside>
  );
}
