import { useEffect, useLayoutEffect, useRef } from "react";
import { api, type AgentSession, type SessionEvent } from "../api";
import { useSessionStream } from "../useSessionStream";
import { Composer } from "./Composer";
import { StatusBadge } from "./StatusBadge";
import { Transcript } from "./Transcript";

interface Props {
  session: AgentSession;
  onChanged: () => void;
  onDeleted: () => void;
}

export function SessionView({ session, onChanged, onDeleted }: Props) {
  const { events, status: liveStatus, connected } = useSessionStream(session.id);
  const status = liveStatus ?? session.status;
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  // Keep the sidebar in step when the live status changes.
  useEffect(() => {
    if (liveStatus && liveStatus !== session.status) onChanged();
  }, [liveStatus]);

  // Follow new events unless the user scrolled up to read.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [events.length, status]);

  const started = lastOf(events, "started");
  const rateLimit = lastOf(events, "rate_limit");

  async function remove() {
    if (!confirm(`Delete "${session.title}" and its history? Files in the project folder are not touched.`)) return;
    await api.deleteSession(session.id);
    onDeleted();
  }

  return (
    <section className="session-view">
      <header className="session-header">
        <div className="session-heading">
          <h1>{session.title}</h1>
          <div className="session-meta">
            <span className="chip">{session.agent}</span>
            <span className="chip">{started?.payload.model ?? session.model ?? "default model"}</span>
            <span className="path" title={session.project_path}>
              {session.project_path}
            </span>
          </div>
        </div>
        <div className="session-actions">
          {rateLimit && <UsageMeter windows={rateLimit.payload.windows} />}
          {!connected && <span className="muted small">Reconnecting…</span>}
          <StatusBadge status={status} />
          <button className="button subtle" onClick={() => void remove()} title="Delete session">
            Delete
          </button>
        </div>
      </header>

      <div
        className="transcript-scroller"
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        {events.length === 0 && status === "ready" ? (
          <div className="empty-state">
            <p>Send a message to start Claude in</p>
            <code>{session.project_path}</code>
          </div>
        ) : (
          <Transcript events={events} status={status} projectPath={session.project_path} />
        )}
      </div>

      <Composer
        status={status}
        onSend={async (text) => {
          stickToBottom.current = true;
          await api.sendMessage(session.id, text);
          onChanged();
        }}
        onStop={async () => {
          await api.stop(session.id);
          onChanged();
        }}
      />
    </section>
  );
}

function UsageMeter({ windows }: { windows?: Record<string, { utilization?: number }> }) {
  if (!windows) return null;
  const labels: Record<string, string> = { five_hour: "5h", seven_day: "7d" };
  return (
    <span className="usage-meter" title="Share of your plan's usage limits used">
      {Object.entries(windows).map(([key, w]) =>
        typeof w.utilization === "number" ? (
          <span key={key} className="usage-window">
            {labels[key] ?? key}
            <span className="usage-bar">
              <span style={{ width: `${Math.min(100, w.utilization * 100)}%` }} />
            </span>
            {Math.round(w.utilization * 100)}%
          </span>
        ) : null,
      )}
    </span>
  );
}

function lastOf(events: SessionEvent[], type: string): SessionEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].type === type) return events[i];
  return undefined;
}
