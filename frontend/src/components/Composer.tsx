import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { AGENT_NAMES, type AgentKind, type SessionStatus } from "../api";

interface Props {
  status: SessionStatus;
  agent: AgentKind;
  onSend: (text: string) => Promise<void>;
  onStop: () => Promise<void>;
  // Replaces the placeholder for the status (a new chat's, for example).
  placeholder?: string;
  // The pickers under the text box: folder, auto handoff.
  controls?: ReactNode;
  // The model picker, shown by the Send button.
  picker?: ReactNode;
  // Opened by a picker (the folder browser), between the text box and the pickers.
  panel?: ReactNode;
  // Hints and warnings about the pickers' choices.
  notes?: ReactNode;
  // A picker's choice can't be used (the local model isn't available): Send stays off.
  sendBlocked?: boolean;
}

const PLACEHOLDERS: Partial<Record<SessionStatus, string>> = {
  ready: "Describe the task…",
  waiting_for_input: "Reply to {agent}…",
  completed: "Continue the conversation…",
  failed: "Try again or continue…",
};
// The box grows with its text up to this height, then scrolls.
const MAX_HEIGHT = 280;

export function Composer({ status, agent, onSend, onStop, placeholder, controls, picker, panel, notes, sendBlocked }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const working = status === "working" || status === "handing_off";
  const agentName = AGENT_NAMES[agent] ?? agent;

  useEffect(() => {
    if (!working) input.current?.focus();
  }, [working]);

  // Fit the box to its text: reset the height first so it can shrink after a deletion.
  useLayoutEffect(() => {
    const el = input.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [text]);

  async function send() {
    const message = text.trim();
    if (!message || working || busy || sendBlocked) return;
    setBusy(true);
    setError(null);
    try {
      await onSend(message);
      setText("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    setBusy(true);
    try {
      await onStop();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`composer${status === "waiting_for_input" ? " attention" : ""}`}>
      {error && <div className="composer-error">{error}</div>}
      <textarea
        ref={input}
        value={text}
        rows={2}
        aria-label={`Message to ${agentName}`}
        placeholder={
          placeholder ??
          (status === "handing_off"
            ? `Handing off to ${agentName}…`
            : working
              ? `${agentName} is working…`
              : PLACEHOLDERS[status]?.replace("{agent}", agentName))
        }
        disabled={working}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void send();
          }
        }}
      />
      {panel && <div className="composer-panel">{panel}</div>}
      {notes && <div className="composer-notes">{notes}</div>}
      <div className="composer-actions">
        <div className="composer-controls">{controls}</div>
        {picker}
        {working ? (
          <button className="button danger" onClick={() => void stop()} disabled={busy}>
            Stop
          </button>
        ) : (
          <button className="button send" onClick={() => void send()} disabled={busy || !text.trim() || sendBlocked}>
            Send
          </button>
        )}
      </div>
    </div>
  );
}
