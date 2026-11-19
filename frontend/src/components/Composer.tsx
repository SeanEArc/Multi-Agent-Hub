import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { AGENT_NAMES, type AgentKind, type SessionStatus, type SlashCommand } from "../api";
import { ACCEPT, attachProblem, namePasted } from "../attachments";
import { PendingChips } from "./Attachments";
import { SlashMenu, matchCommands } from "./SlashMenu";

interface Props {
  status: SessionStatus;
  agent: AgentKind;
  // `files` were attached in the composer (button, paste or drop); the caller uploads them with the message.
  onSend: (text: string, files: File[]) => Promise<void>;
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
  // Loads the `/` menu (the agent's skills and commands); called the first time "/" is typed for `commandsKey`.
  loadCommands?: () => Promise<SlashCommand[]>;
  // Changes when the menu would differ (another agent or folder), so it is loaded again.
  commandsKey?: string;
}

const PLACEHOLDERS: Partial<Record<SessionStatus, string>> = {
  ready: "Describe the task…",
  waiting_for_input: "Reply to {agent}…",
  completed: "Continue the conversation…",
  failed: "Try again or continue…",
};
// The box grows with its text up to this height, then scrolls.
const MAX_HEIGHT = 280;
// Files per message (the backend allows 20).
const MAX_FILES = 20;

export function Composer({
  status,
  agent,
  onSend,
  onStop,
  placeholder,
  controls,
  picker,
  panel,
  notes,
  sendBlocked,
  loadCommands,
  commandsKey,
}: Props) {
  const [text, setText] = useState("");
  // The `/` menu: loaded on first use per commandsKey; open while the message is just "/" and a name.
  const [commands, setCommands] = useState<SlashCommand[] | null>(null);
  const [commandsError, setCommandsError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState(false);
  const loadedFor = useRef<string | null>(null);
  const slash = /^\/(\S*)$/.exec(text);
  const menuOpen = !!loadCommands && !!slash && !dismissed;
  const query = slash?.[1] ?? "";
  const matches = commands ? matchCommands(commands, query) : [];

  useEffect(() => {
    if (!menuOpen || !loadCommands) return;
    const key = commandsKey ?? agent;
    if (loadedFor.current === key) return;
    loadedFor.current = key;
    setCommands(null);
    setCommandsError(null);
    let current = true;
    loadCommands().then(
      (list) => current && setCommands(list),
      (e) => {
        if (!current) return;
        loadedFor.current = null;
        setCommandsError(e instanceof Error ? e.message : String(e));
      },
    );
    return () => {
      current = false;
    };
  }, [menuOpen, loadCommands, commandsKey, agent]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    if (!slash) setDismissed(false);
  }, [slash]);

  function pick(command: SlashCommand) {
    setText(`/${command.name} `);
    input.current?.focus();
  }
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  // Attached files, sent with the next message; a drag over the window shows the drop hint.
  const [files, setFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const filePicker = useRef<HTMLInputElement>(null);
  const filesRef = useRef<File[]>(files);
  filesRef.current = files;

  function addFiles(list: Iterable<File>) {
    const problems: string[] = [];
    const accepted: File[] = [];
    for (const file of list) {
      const problem = attachProblem(file);
      if (problem) problems.push(problem);
      else accepted.push(file);
    }
    // The drop listener is set up once, so the current count comes from a ref, not this render's state.
    const room = Math.max(0, MAX_FILES - filesRef.current.length);
    if (accepted.length > room) problems.push(`Up to ${MAX_FILES} files can go with one message.`);
    const added = accepted.slice(0, room);
    filesRef.current = [...filesRef.current, ...added];
    setFiles(filesRef.current);
    setError(problems.length ? problems.join(" ") : null);
  }

  // Dropping files anywhere on the window attaches them (and keeps the browser from opening them).
  useEffect(() => {
    const hasFiles = (e: DragEvent) => !!e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files");
    const over = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setDragging(true);
    };
    const leave = (e: DragEvent) => {
      if (!e.relatedTarget) setDragging(false);
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setDragging(false);
      addFiles(Array.from(e.dataTransfer?.files ?? []));
      input.current?.focus();
    };
    window.addEventListener("dragenter", over);
    window.addEventListener("dragover", over);
    window.addEventListener("dragleave", leave);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", over);
      window.removeEventListener("dragover", over);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("drop", drop);
    };
  }, []);
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
    if ((!message && !files.length) || working || busy || sendBlocked) return;
    setBusy(true);
    setError(null);
    try {
      await onSend(message, files);
      setText("");
      setFiles([]);
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
    <div className={`composer${status === "waiting_for_input" ? " attention" : ""}${dragging ? " dragging" : ""}`}>
      {dragging && (
        <div className="drop-overlay" aria-hidden="true">
          <span>Drop files to attach them</span>
        </div>
      )}
      {error && <div className="composer-error">{error}</div>}
      <PendingChips files={files} disabled={busy} onRemove={(index) => setFiles((current) => current.filter((_, i) => i !== index))} />
      {menuOpen && (
        <SlashMenu commands={commands} error={commandsError} query={query} active={active} onPick={pick} onHover={setActive} />
      )}
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
        onPaste={(e) => {
          // A screenshot or files copied in Explorer; plain text pastes as usual.
          const pasted = Array.from(e.clipboardData.files);
          if (!pasted.length) return;
          e.preventDefault();
          addFiles(pasted.map(namePasted));
        }}
        onKeyDown={(e) => {
          if (menuOpen && matches.length > 0) {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              const step = e.key === "ArrowDown" ? 1 : -1;
              setActive((active + step + matches.length) % matches.length);
              return;
            }
            if ((e.key === "Enter" && !e.shiftKey) || e.key === "Tab") {
              e.preventDefault();
              pick(matches[Math.min(active, matches.length - 1)]);
              return;
            }
          }
          if (menuOpen && e.key === "Escape") {
            e.preventDefault();
            setDismissed(true);
            return;
          }
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void send();
          }
        }}
      />
      {panel && <div className="composer-panel">{panel}</div>}
      {notes && <div className="composer-notes">{notes}</div>}
      <div className="composer-actions">
        <button
          type="button"
          className="attach-button"
          title="Attach files: images, PDF, Word, Excel, CSV, text or code (or paste a screenshot, or drop files here)"
          aria-label="Attach files"
          disabled={busy}
          onClick={() => filePicker.current?.click()}
        >
          <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
            <path
              d="M21.4 11.1l-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.8-7.8"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
        <input
          ref={filePicker}
          type="file"
          multiple
          accept={ACCEPT}
          hidden
          onChange={(e) => {
            addFiles(Array.from(e.target.files ?? []));
            e.target.value = "";
          }}
        />
        <div className="composer-controls">{controls}</div>
        {picker}
        {working ? (
          <button className="button danger" onClick={() => void stop()} disabled={busy}>
            Stop
          </button>
        ) : (
          <button
            className="button send"
            onClick={() => void send()}
            disabled={busy || (!text.trim() && !files.length) || sendBlocked}
          >
            {busy && files.length ? "Uploading…" : "Send"}
          </button>
        )}
      </div>
    </div>
  );
}
