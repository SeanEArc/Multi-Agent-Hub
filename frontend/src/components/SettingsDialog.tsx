import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type HubSettings, type LimitPolicy, type ProgressLayout } from "../api";
import { ChainEditor, POLICY_LABELS } from "./ChainEditor";

interface Props {
  onClose: () => void;
}

/** Defaults for sessions that don't set their own handoff chain or limit policy, and how sessions are shown. */
export function SettingsDialog({ onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [settings, setSettings] = useState<HubSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    dialog.current?.showModal();
    api.getSettings().then(setSettings, (e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!settings) return;
    setBusy(true);
    setError(null);
    try {
      await api.putSettings(settings);
      dialog.current?.close();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose}>
      <form onSubmit={submit}>
        <h2>Settings</h2>
        {settings ? (
          <>
            <div className="field">
              <span>Default handoff chain</span>
              <ChainEditor chain={settings.default_chain} onChange={(default_chain) => setSettings({ ...settings, default_chain })} />
              <span className="hint">
                When an agent hits its usage limit, the next agent below it takes over from a brief of the task and the
                git diff. Sessions can override this.
              </span>
            </div>
            <label>
              When an agent hits its limit
              <select
                value={settings.limit_policy}
                onChange={(e) => setSettings({ ...settings, limit_policy: e.target.value as LimitPolicy })}
              >
                {Object.entries(POLICY_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Show a session's plan and subagents
              <select
                value={settings.progress_layout}
                onChange={(e) => setSettings({ ...settings, progress_layout: e.target.value as ProgressLayout })}
              >
                <option value="panel">In a panel above the transcript</option>
                <option value="split">In a side panel, with the files changed</option>
              </select>
            </label>
          </>
        ) : (
          !error && <p className="muted">Loading…</p>
        )}

        {error && <div className="composer-error">{error}</div>}

        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy || !settings}>
            Save
          </button>
        </div>
      </form>
    </dialog>
  );
}
