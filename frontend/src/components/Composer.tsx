import { useEffect, useRef, useState } from "react";
import type { SessionStatus } from "../api";

interface Props {
  status: SessionStatus;
  onSend: (text: string) => Promise<void>;
  onStop: () => Promise<void>;
}

const PLACEHOLDERS: Partial<Record<SessionStatus, string>> = {
  ready: "Describe the task…",
  waiting_for_input: "Reply to Claude…",
  completed: "Continue the conversation…",
  failed: "Try again or continue…",
};

export function Composer({ status, onSend, onStop }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const working = status === "working" || status === "handing_off";

  useEffect(() => {
    if (!working) input.current?.focus();
  }, [working]);

  async function send() {
    const message = text.trim();
    if (!message || working || busy) return;
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
        rows={3}
        placeholder={working ? "Claude is working…" : PLACEHOLDERS[status]}
        disabled={working}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void send();
          }
        }}
      />
      <div className="composer-actions">
        <span className="muted small">Enter to send · Shift+Enter for a new line</span>
        {working ? (
          <button className="button danger" onClick={() => void stop()} disabled={busy}>
            Stop
          </button>
        ) : (
          <button className="button primary" onClick={() => void send()} disabled={busy || !text.trim()}>
            Send
          </button>
        )}
      </div>
    </div>
  );
}
