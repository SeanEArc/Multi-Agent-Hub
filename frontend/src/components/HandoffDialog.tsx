import { useEffect, useRef, useState, type FormEvent } from "react";
import { AGENT_NAMES, AVAILABLE_AGENTS, api, type AgentKind, type AgentSession } from "../api";
import { ModelSelect, localProblem, useLocalModels } from "./ModelSelect";

interface Props {
  session: AgentSession;
  onDone: () => void;
  onClose: () => void;
}

/** Moves the session to another agent, which starts right away from the hub's brief. */
export function HandoffDialog({ session, onDone, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [agent, setAgent] = useState<AgentKind>(AVAILABLE_AGENTS.find((a) => a !== session.agent) ?? session.agent);
  const [model, setModel] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const local = useLocalModels(agent === "ollama");
  const problem = agent === "ollama" ? localProblem(local) : null;

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.handoff(session.id, { agent, model: model || null, note: note.trim() || null });
      onDone();
      dialog.current?.close();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose}>
      <form onSubmit={submit}>
        <h2>Hand off</h2>
        <p className="muted small">
          The new agent starts a fresh session from a brief the hub writes: the task, your messages, what{" "}
          {AGENT_NAMES[session.agent]} said and did last, and the git diff.
        </p>

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
              {(["claude", "codex", "ollama"] as AgentKind[]).map((a) => (
                <option key={a} value={a} disabled={!AVAILABLE_AGENTS.includes(a)}>
                  {AGENT_NAMES[a]}
                  {a === session.agent ? " (restart from brief)" : ""}
                  {AVAILABLE_AGENTS.includes(a) ? "" : " (phase 5)"}
                </option>
              ))}
            </select>
          </label>
          <label>
            Model
            <ModelSelect agent={agent} value={model || null} onChange={(m) => setModel(m ?? "")} local={local} />
          </label>
        </div>
        {agent === "ollama" && (
          <span className={`hint${problem ? " warning" : ""}`}>
            {local === null
              ? "Looking for Ollama…"
              : problem ??
                "Runs on this PC with no subscription. Local models are slower and weaker at coding; a long brief may be shortened to fit the model's context."}
          </span>
        )}

        <label>
          <span>
            Instructions <span className="hint">(optional)</span>
          </span>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={3}
            placeholder="What should it do next? Leave empty to continue the task."
          />
        </label>

        {error && <div className="composer-error">{error}</div>}

        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy || !!problem}>
            Hand off to {AGENT_NAMES[agent]}
          </button>
        </div>
      </form>
    </dialog>
  );
}
