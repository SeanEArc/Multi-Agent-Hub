import { useCallback, useEffect, useState } from "react";
import { api, type AgentSession, type HubSettings, type ProgressLayout } from "./api";
import { Dashboard } from "./components/Dashboard";
import { NewChatView } from "./components/NewChatView";
import { SessionView } from "./components/SessionView";
import { SettingsDialog } from "./components/SettingsDialog";
import { Sidebar } from "./components/Sidebar";
import { BackendBanner } from "./components/BackendBanner";
import { inDesktopApp } from "./connection";

const SELECTED_KEY = "hub.selectedSession";
// The dashboard polls often; with a session open, that session streams live and the rest poll slower.
const DASHBOARD_POLL_MS = 5000;
const POLL_MS = 20000;

export function App() {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(() => readSelected());
  // A new session before its first message, shown instead of the selected session or the dashboard.
  const [drafting, setDrafting] = useState(false);
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

  const selected = sessions.find((s) => s.id === selectedId) ?? null;

  function select(id: string | null) {
    setDrafting(false);
    setSelectedId(id);
  }

  function startNew() {
    setSelectedId(null);
    setDrafting(true);
  }

  // Hub settings, for the progress layout; the settings dialog loads its own copy.
  const [settings, setSettings] = useState<HubSettings | null>(null);
  const loadSettings = useCallback(() => {
    api.getSettings().then(setSettings, () => undefined);
  }, []);
  useEffect(loadSettings, [loadSettings]);

  async function changeLayout(progress_layout: ProgressLayout) {
    if (!settings) return;
    setSettings({ ...settings, progress_layout });
    try {
      setSettings(await api.putSettings({ ...settings, progress_layout }));
    } catch {
      loadSettings();
    }
  }

  // The sidebar and dashboard poll so every session's status stays current.
  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), selected ? POLL_MS : DASHBOARD_POLL_MS);
    return () => clearInterval(timer);
  }, [refresh, selected === null]);

  useEffect(() => {
    try {
      if (selectedId) localStorage.setItem(SELECTED_KEY, selectedId);
      else localStorage.removeItem(SELECTED_KEY);
    } catch {
      // storage unavailable; selection just won't be remembered
    }
  }, [selectedId]);

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        selectedId={selectedId}
        drafting={drafting}
        onSelect={select}
        onNew={startNew}
        onSettings={() => setEditingSettings(true)}
      />
      <main className="main">
        {inDesktopApp && <BackendBanner unreachable={backendDown} />}
        {backendDown && !inDesktopApp && (
          <div className="banner error">
            Can't reach the backend. Start it with <code>uv run python -m app.main</code> in the <code>backend</code>{" "}
            folder.
          </div>
        )}
        {drafting ? (
          <NewChatView
            settings={settings}
            // Sessions come newest first, so the Set keeps that order.
            recentProjects={[...new Set(sessions.filter((s) => !s.chat).map((s) => s.project_path))]}
            onCreated={async (id) => {
              await refresh();
              select(id);
            }}
          />
        ) : selected ? (
          <SessionView
            key={selected.id}
            session={selected}
            onChanged={() => void refresh()}
            settings={settings}
            onDeleted={() => {
              setSelectedId(null);
              void refresh();
            }}
            layout={settings?.progress_layout ?? "panel"}
            onLayoutChange={(layout) => void changeLayout(layout)}
          />
        ) : (
          <Dashboard sessions={sessions} onSelect={select} onNew={startNew} />
        )}
      </main>

      {editingSettings && (
        <SettingsDialog
          onClose={() => {
            setEditingSettings(false);
            loadSettings();
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
