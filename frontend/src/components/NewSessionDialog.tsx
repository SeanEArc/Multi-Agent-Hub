import { useEffect, useRef, useState, type FormEvent } from "react";
import type { AgentKind, NewSession } from "../api";
import { FolderPicker } from "./FolderPicker";

interface Props {
  onCreate: (session: NewSession, firstMessage: string) => Promise<void>;
  onClose: () => void;
  // Project folders of existing sessions, newest first, offered as shortcuts.
  recentProjects: string[];
}

// Values each CLI accepts for --model; empty means the CLI's default.
// Codex slugs come from its model cache (codex-cli 0.164.0).
const MODELS: Partial<Record<AgentKind, string[]>> = {
  claude: ["", "fable", "opus", "sonnet", "haiku"],
  codex: ["", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"],
};

export function NewSessionDialog({ onCreate, onClose, recentProjects }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [agent, setAgent] = useState<AgentKind>("claude");
  const [model, setModel] = useState("");
  const [firstMessage, setFirstMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onCreate(
        { title: title.trim(), project_path: projectPath.trim(), agent, model: model || null },
        firstMessage.trim(),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose}>
      <form onSubmit={submit}>
        <h2>New session</h2>

        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus placeholder="Fix login bug" />
        </label>

        <div className="field">
          <label htmlFor="project-path">Project folder</label>
          <FolderPicker value={projectPath} onChange={setProjectPath} recent={recentProjects} />
          <span className="hint">Type or paste a path, or browse to the folder. The agent can edit files in it.</span>
        </div>

        <div className="field-row">
          <label>
            Agent
            <select
              value={agent}
              onChange={(e) => {
                setAgent(e.target.value as AgentKind);
                setModel("");
              }}
            >
              <option value="claude">Claude Code</option>
              <option value="codex">Codex</option>
              <option value="ollama" disabled>
                Local model (phase 5)
              </option>
            </select>
          </label>
          <label>
            Model
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {(MODELS[agent] ?? [""]).map((m) => (
                <option key={m} value={m}>
                  {m || "Default"}
                </option>
              ))}
            </select>
          </label>
        </div>

        <label>
          <span>
            First message <span className="hint">(optional)</span>
          </span>
          <textarea
            value={firstMessage}
            onChange={(e) => setFirstMessage(e.target.value)}
            rows={4}
            placeholder="Describe the task. You can also send it later."
          />
        </label>

        {error && <div className="composer-error">{error}</div>}

        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy}>
            {firstMessage.trim() ? "Create and start" : "Create"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
