import { useEffect, useRef, useState, type FormEvent } from "react";
import { EFFORTS, EFFORT_NAMES, api, type AgentKind, type Effort, type CatalogAgent, type HubSettings, type ModelCatalog, type ProgressLayout } from "../api";
import { useTheme } from "../theme";
import { ChainEditor } from "./ChainEditor";
import { AGENT_OPTIONS } from "./ComposerControls";
import { ModelSelect, firstModel, localProblem, useLocalModels, useModelCatalog } from "./ModelSelect";

interface Props {
  onClose: () => void;
}

/** Defaults for new sessions, chaining and handoff, and how sessions are shown. */
export function SettingsDialog({ onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [settings, setSettings] = useState<HubSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useTheme();
  const local = useLocalModels(true);
  const { catalog } = useModelCatalog();
  // Older backends don't send it: chaining was always on.
  const chaining = settings?.chaining_enabled ?? true;
  const defaultAgent = settings?.default_agent ?? "claude";
  // A named model is always chosen; one saved before this (or never set) shows the agent's first model.
  const defaultModel = settings?.default_model ?? firstModel(defaultAgent, local, catalog);

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
      await api.putSettings({ ...settings, default_model: defaultModel });
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
        <div className="field">
          <span>Theme</span>
          <div className="layout-switch" role="group" aria-label="Theme">
            {(["system", "light", "dark"] as const).map((option) => (
              <button key={option} type="button" aria-pressed={theme === option} onClick={() => setTheme(option)}>
                {option === "system" ? "System" : option === "light" ? "Light" : "Dark"}
              </button>
            ))}
          </div>
          <span className="hint">System follows the Windows light or dark setting. Applies right away, in this browser.</span>
        </div>
        {settings ? (
          <>
            <div className="field">
              <span>New sessions start with</span>
              <div className="field-row">
                <select
                  aria-label="Default agent"
                  value={defaultAgent}
                  onChange={(e) => {
                    const agent = e.target.value as AgentKind;
                    setSettings({ ...settings, default_agent: agent, default_model: firstModel(agent, local, catalog) });
                  }}
                >
                  {AGENT_OPTIONS.map((o) => (
                    <option key={o.agent} value={o.agent}>
                      {o.label}
                    </option>
                  ))}
                </select>
                <ModelSelect
                  aria-label="Default model"
                  agent={defaultAgent}
                  value={defaultModel}
                  local={local}
                  noDefault
                  onChange={(default_model) => setSettings({ ...settings, default_model })}
                />
                {defaultAgent !== "ollama" && (
                  <select
                    aria-label="Default effort"
                    title="Reasoning effort"
                    value={settings.default_effort ?? "medium"}
                    onChange={(e) => setSettings({ ...settings, default_effort: e.target.value as Effort })}
                  >
                    {EFFORTS.map((effort) => (
                      <option key={effort} value={effort}>
                        {EFFORT_NAMES[effort]} effort
                      </option>
                    ))}
                  </select>
                )}
              </div>
              <span className={`hint${settings.default_agent === "ollama" && localProblem(local) ? " warning" : ""}`}>
                {settings.default_agent === "ollama" && localProblem(local)
                  ? localProblem(local)
                  : "The agent, model and reasoning effort a new session starts with (Claude and Codex also use this effort when they take over in a handoff). You can change them in the box by Send."}
              </span>
            </div>
            <ModelCheck />

            <div className="field">
              <label className="checkbox">
                <input
                  type="checkbox"
                  role="switch"
                  checked={chaining}
                  onChange={(e) => setSettings({ ...settings, chaining_enabled: e.target.checked })}
                />
                Allow chaining
              </label>
              {!chaining && (
                <span className="hint">
                  A usage limit stops the session and waits for you; no other agent takes over. You can still switch
                  agents yourself under the text box.
                </span>
              )}
            </div>
            {chaining && (
              <>
                <div className="field">
                  <span>Default handoff chain</span>
                  <ChainEditor chain={settings.default_chain} onChange={(default_chain) => setSettings({ ...settings, default_chain })} />
                  <span className="hint">
                    When an agent hits its usage limit, the next agent below it takes over from a brief of the task and
                    the git diff.
                  </span>
                </div>
                <div className="field">
                  <label className="checkbox">
                    <input
                      type="checkbox"
                      role="switch"
                      checked={settings.limit_policy === "auto"}
                      onChange={(e) => setSettings({ ...settings, limit_policy: e.target.checked ? "auto" : "ask" })}
                    />
                    Auto handoff by default
                  </label>
                  <span className="hint">
                    {settings.limit_policy === "auto"
                      ? "New sessions start with Auto handoff on: the next agent takes over at once when usage runs out."
                      : "New sessions start with Auto handoff off: the hub offers the next agent and waits for you."}{" "}
                    Each session can change it under the text box.
                  </span>
                </div>
              </>
            )}
            <label>
              Default local model
              <ModelSelect
                agent="ollama"
                value={settings.local_model}
                local={local && { ...local, default: local.default && !settings.local_model ? local.default : null }}
                onChange={(local_model) => setSettings({ ...settings, local_model })}
              />
              <span className="hint">
                {local && !local.available
                  ? local.error
                  : "Used by local sessions, chain entries and handoffs that don't pick a model. Default: the first installed model that can use tools."}
              </span>
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

const CHECK_AGENTS: { agent: CatalogAgent; label: string }[] = [
  { agent: "claude", label: "Claude Code" },
  { agent: "codex", label: "Codex" },
];

/** "11 available, 2 not on your plan, 1 not checked yet". */
function checkSummary(catalog: ModelCatalog, agent: CatalogAgent): string {
  const models = catalog[agent] ?? [];
  const yes = models.filter((m) => m.available === true).length;
  const no = models.filter((m) => m.available === false).length;
  const unknown = models.length - yes - no;
  const parts = [`${yes} available`];
  if (no) parts.push(`${no} not on your plan`);
  if (unknown) parts.push(`${unknown} not checked yet`);
  return parts.join(", ");
}

function formatTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date.toLocaleString();
}

/** Buttons that try each of an agent's models once, so the pickers know which the plan can use. */
function ModelCheck() {
  const { catalog, offline, setCatalog } = useModelCatalog();
  const [running, setRunning] = useState<Partial<Record<CatalogAgent, boolean>>>({});
  const [errors, setErrors] = useState<Partial<Record<CatalogAgent, string | null>>>({});

  async function check(agent: CatalogAgent) {
    setRunning((r) => ({ ...r, [agent]: true }));
    setErrors((e) => ({ ...e, [agent]: null }));
    try {
      setCatalog(await api.checkModels(agent));
    } catch (err) {
      setErrors((e) => ({ ...e, [agent]: err instanceof Error ? err.message : String(err) }));
    } finally {
      setRunning((r) => ({ ...r, [agent]: false }));
    }
  }

  return (
    <div className="field">
      <span>Check which models my plan can use</span>
      <ul className="model-check">
        {CHECK_AGENTS.map(({ agent, label }) => {
          const checked = formatTime(catalog?.checked_at?.[agent]);
          return (
            <li key={agent} className="model-check-row">
              <button
                type="button"
                className="button"
                disabled={!!running[agent] || offline || !catalog}
                onClick={() => void check(agent)}
              >
                {label}
              </button>
              <span className={`hint${errors[agent] ? " warning" : ""}`}>
                {running[agent]
                  ? "Checking… this uses a little of your usage and can take a minute or two."
                  : errors[agent]
                    ? errors[agent]
                    : catalog && !offline
                      ? `${checkSummary(catalog, agent)}${checked ? `. Last checked ${checked}` : ". Not checked yet"}`
                      : offline
                        ? "The model list couldn't be loaded; showing a built-in list."
                        : "Loading…"}
              </span>
            </li>
          );
        })}
      </ul>
      <span className="hint">
        Sends a tiny prompt to each model once. The list also updates by itself as you use models: one your plan
        can't use is marked "not on your plan".{" "}
        {catalog && !offline
          ? catalog.codex_source === "account"
            ? "Codex models come from your Codex account."
            : "Codex's account list wasn't found; showing a built-in list."
          : ""}
      </span>
    </div>
  );
}
