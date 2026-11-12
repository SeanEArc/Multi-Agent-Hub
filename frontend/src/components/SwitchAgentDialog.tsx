import { useEffect, useRef, useState } from "react";
import { AGENT_NAMES, EFFORT_NAMES, api, type AgentKind, type AgentSession, type Effort } from "../api";
import { AgentLogo } from "./AgentLogo";
import { localProblem, orderedModels, useLocalModels, useModelCatalog } from "./ModelSelect";

interface Props {
  session: AgentSession;
  // The agent, model and effort picked in the model box by Send.
  agent: AgentKind;
  model: string | null;
  effort: Effort | null;
  onDone: () => void;
  onClose: () => void;
}

// What the new agent is told about the conversation so far.
type Carry = "brief" | "messages" | "nothing";

/**
 * Confirms switching the session to the agent, model and effort picked in the model box, and asks what to
 * pass along: a full brief, only the user's own earlier messages, or nothing. The new agent waits for the
 * next message.
 */
export function SwitchAgentDialog({ session, agent, model, effort, onDone, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { catalog } = useModelCatalog();
  const local = useLocalModels(agent === "ollama");
  const problem = agent === "ollama" ? localProblem(local) : null;
  const name = AGENT_NAMES[agent];
  const from = AGENT_NAMES[session.agent];
  const modelLabel =
    agent === "claude" || agent === "codex"
      ? (orderedModels(agent, catalog).find((m) => m.id === model)?.label ?? model ?? "Default model")
      : (model ?? local?.default ?? "Default model");

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  async function switchTo(carry: Carry) {
    setBusy(true);
    setError(null);
    try {
      await api.handoff(session.id, {
        agent,
        model,
        effort,
        brief: carry === "brief",
        messages: carry === "messages",
        wait: true,
      });
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
          void switchTo("brief");
        }}
      >
        <h2>{agent === session.agent ? `Switch ${name}'s model?` : `Switch to ${name}?`}</h2>
        <div className="switch-target">
          <AgentLogo agent={agent} />
          <span>{modelLabel}</span>
          {effort && agent !== "ollama" && <span className="model-button-effort">{EFFORT_NAMES[effort]}</span>}
        </div>
        <p className="muted small">What should {name} know about the conversation so far?</p>
        <ul className="muted small switch-options">
          <li>
            <strong>Brief:</strong> the task, your messages, what {from} said and did last, and the git diff.
          </li>
          <li>
            <strong>My messages only:</strong> your earlier messages, without {from}'s replies.
          </li>
          <li>
            <strong>Start clean:</strong> nothing; it sees only the messages that follow.
          </li>
        </ul>
        <p className="muted small">Either way it waits for your next message.</p>
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
          <button type="button" className="button" disabled={busy || !!problem} onClick={() => void switchTo("nothing")}>
            Start clean
          </button>
          <button type="button" className="button" disabled={busy || !!problem} onClick={() => void switchTo("messages")}>
            My messages only
          </button>
          <button type="submit" className="button primary" disabled={busy || !!problem} autoFocus>
            Switch with brief
          </button>
        </div>
      </form>
    </dialog>
  );
}
