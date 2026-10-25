import { useCallback, useEffect, useState } from "react";
import { api, type AgentSession } from "./api";
import { NewSessionDialog } from "./components/NewSessionDialog";
import { SessionView } from "./components/SessionView";
import { SettingsDialog } from "./components/SettingsDialog";
import { Sidebar } from "./components/Sidebar";

const SELECTED_KEY = "hub.selectedSession";
const POLL_MS = 60000;

export function App() {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(() => readSelected());
  const [creating, setCreating] = useState(false);
  const [editingSettings, setEditingSettings] = useState(false);
  const [backendDown, setBackendDown] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
      setBackendDown(false);
    } catch {
      setBackendDown(true);
    }
  }, []);

  // The sidebar polls so other sessions' statuses stay current; the open session streams live.
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    try {
      if (selectedId) localStorage.setItem(SELECTED_KEY, selectedId);
      else localStorage.removeItem(SELECTED_KEY);
    } catch {
      // storage unavailable; selection just won't be remembered
    }
  }, [selectedId]);

  const selected = sessions.find((s) => s.id === selectedId) ?? null;

  return (
    <div className="app">
      <Sidebar sessions={sessions} selectedId={selectedId} onSelect={setSelectedId} onNew={() => setCreating(true)}
        onSettings={() => setEditingSettings(true)}
      />
      <main className="main">
        {backendDown && (
          <div className="banner error">
            Can't reach the backend. Start it with <code>uv run python -m app.main</code> in the <code>backend</code>{" "}
            folder.
          </div>
        )}
        {selected ? (
          <SessionView
            key={selected.id}
            session={selected}
            onChanged={() => void refresh()}
            onDeleted={() => {
              setSelectedId(null);
              void refresh();
            }}
          />
        ) : (
          <div className="empty-state">
            <h2>No session selected</h2>
            <p className="muted">Pick a session on the left, or start a new one.</p>
            <button className="button primary" onClick={() => setCreating(true)}>
              + New session
            </button>
          </div>
        )}
      </main>

      {editingSettings && <SettingsDialog onClose={() => setEditingSettings(false)} />}

      {creating && (
        <NewSessionDialog
          onClose={() => setCreating(false)}
          // Sessions come newest first, so the Set keeps that order.
          recentProjects={[...new Set(sessions.map((s) => s.project_path))]}
          onCreate={async (body, firstMessage) => {
            const session = await api.createSession(body);
            if (firstMessage) await api.sendMessage(session.id, firstMessage);
            await refresh();
            setSelectedId(session.id);
            setCreating(false);
          }}
        />
      )}
    </div>
  );
}

function readSelected(): string | null {
  try {
    return localStorage.getItem(SELECTED_KEY);
  } catch {
    return null;
  }
}
