import type { AgentSession } from "../api";
import { StatusDot } from "./StatusBadge";

interface Props {
  sessions: AgentSession[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}

export function Sidebar({ sessions, selectedId, onSelect, onNew }: Props) {
  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <span className="brand">Multi-Agent Hub</span>
      </div>
      <button className="button primary new-session" onClick={onNew}>
        + New session
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
                {session.agent}
                {session.model ? ` · ${session.model}` : ""} · {folderName(session.project_path)}
              </span>
            </span>
          </button>
        ))}
      </nav>
    </aside>
  );
}

function folderName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path;
}
