import { useEffect, useRef, useState } from "react";
import { AGENT_NAMES, api, type AgentKind, type AgentSession, type Effort } from "../api";
import { ModelSelect, localProblem, useLocalModels } from "./ModelSelect";

interface Props {
  session: AgentSession;
  // The agent and model picked under the text box.
  agent: AgentKind;
  model: string | null;
  // The reasoning effort picked with it (null: the CLI's default).
  effort: Effort | null;
  onDone: () => void;
  onClose: () => void;
}

/** Confirms switching the session to another agent or model, with or without a brief; the new agent waits for the next message. */
export function SwitchAgentDialog({ session, agent, model: picked, effort, onDone, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [model, setModel] = useState<string | null>(picked);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const local = useLocalModels(agent === "ollama");
  const problem = agent === "ollama" ? localProblem(local) : null;
  const name = AGENT_NAMES[agent];
  const from = AGENT_NAMES[session.agent];

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  async function switchTo(brief: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api.handoff(session.id, { agent, model, effort, brief, wait: true });
      onDone();
      dialog.current?.close();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void switchTo(true);
        }}
      >
        <h2>{agent === session.agent ? `Switch ${name}'s model?` : `Switch to ${name}?`}</h2>
        <p className="muted small">
          Pass along a brief of the conversation and tasks so far? With a brief, {name} gets the task, your messages,
          what {from} said and did last, and the git diff. Without one, it starts clean and sees only the messages that
          follow. Either way it waits for your next message.
        </p>
        <label>
          Model
          <ModelSelect agent={agent} value={model} onChange={setModel} local={local} />
        </label>
        {agent === "ollama" && (
          <span className={`hint${problem ? " warning" : ""}`}>
            {local === null
              ? "Looking for Ollama…"
              : problem ?? "A long brief may be shortened to fit the local model's context."}
          </span>
        )}

        {error && <div className="composer-error">{error}</div>}

        <div className="dialog-actions wrap">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            Cancel
          </button>
          <button type="button" className="button" disabled={busy || !!problem} onClick={() => void switchTo(false)}>
            Switch without brief
          </button>
          <button type="submit" className="button primary" disabled={busy || !!problem} autoFocus>
            Switch with brief
          </button>
        </div>
      </form>
    </dialog>
  );
}
