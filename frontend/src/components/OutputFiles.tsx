// Files agents wrote to the session's outputs folder (backend/app/files/outputs.py): a list in the side panel, and cards in
// the transcript where each file was created or edited. Both have Preview, Open and Save as….
import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { api, type OutputFile } from "../api";
import { kindOf, sizeText, useApiBlob, useObjectUrl } from "../attachments";
import type { OutputChange } from "../outputs";
import { outputPreview, outputUrl, usePreview } from "../preview";
import { BADGES } from "./Attachments";
import { FolderPicker } from "./FolderPicker";

interface OutputsState {
  sessionId: string;
  files: OutputFile[];
  saveAs: (file: OutputFile) => void;
}

const OutputsContext = createContext<OutputsState | null>(null);

export function useOutputs(): OutputsState | null {
  return useContext(OutputsContext);
}

/** The outputs folder's files, loaded again whenever `refreshKey` changes. */
export function useOutputList(sessionId: string, refreshKey: string): OutputFile[] {
  const [files, setFiles] = useState<OutputFile[]>([]);
  useEffect(() => {
    let current = true;
    api.listOutputs(sessionId).then(
      (list) => current && setFiles(list),
      () => undefined, // an older backend has no outputs: show nothing
    );
    return () => {
      current = false;
    };
  }, [sessionId, refreshKey]);
  return files;
}

/** Gives the side panel and the transcript the session's files, and holds their Save as dialog. */
export function OutputsProvider({ sessionId, files, children }: { sessionId: string; files: OutputFile[]; children: ReactNode }) {
  const [saving, setSaving] = useState<OutputFile | null>(null);
  return (
    <OutputsContext.Provider value={{ sessionId, files, saveAs: setSaving }}>
      {children}
      {saving && <SaveAsDialog sessionId={sessionId} file={saving} onClose={() => setSaving(null)} />}
    </OutputsContext.Provider>
  );
}

/** The side panel's list, newest first. */
export function OutputFiles() {
  const outputs = useOutputs();
  if (!outputs?.files.length) return null;
  const newestFirst = [...outputs.files].sort((a, b) => b.modified - a.modified);
  return (
    <details className="outputs-panel" open>
      <summary>
        Files from agents <span className="muted">· {newestFirst.length}</span>
      </summary>
      <ul className="outputs-list">
        {newestFirst.map((file) => (
          <li key={file.name} className="output-row" title={file.path}>
            <OutputDetails file={file} />
          </li>
        ))}
      </ul>
    </details>
  );
}

// A turn can write many files (a page image each): the rest show on request.
const CHAT_CARDS = 4;

/** In the transcript: the files a tool call (or the turn's end) created or edited. */
export function ChatOutputs({ changes }: { changes: OutputChange[] }) {
  const outputs = useOutputs();
  const [all, setAll] = useState(false);
  if (!outputs) return null;
  const shown = all ? changes : changes.slice(0, CHAT_CARDS);
  return (
    <div className="row chat-outputs">
      {shown.map((change) => {
        const file = outputs.files.find((f) => f.name === change.name);
        return (
          <div key={change.name} className="output-row chat-output" title={file?.path}>
            {file && kindOf(file.name) === "image" && <OutputImage sessionId={outputs.sessionId} file={file} />}
            {file ? (
              <OutputDetails file={file} change={change.change} />
            ) : (
              <>
                <span className="output-name">{change.name}</span>
                <span className="muted output-details">{change.change}</span>
              </>
            )}
          </div>
        );
      })}
      {changes.length > CHAT_CARDS && (
        <button type="button" className="button subtle chat-outputs-more" onClick={() => setAll(!all)} aria-expanded={all}>
          {all ? "Show fewer" : `Show ${changes.length - CHAT_CARDS} more`}
        </button>
      )}
    </div>
  );
}

// Larger pictures are shown by name only (attached images are capped at the same size).
const INLINE_IMAGE_BYTES = 20 * 1024 * 1024;

function OutputImage({ sessionId, file }: { sessionId: string; file: OutputFile }) {
  // The time makes an overwritten picture load again; the backend ignores it.
  const path = file.size > INLINE_IMAGE_BYTES ? null : `${outputUrl(sessionId, file.name)}&v=${file.modified}`;
  const url = useObjectUrl(useApiBlob(path).blob);
  const preview = usePreview();
  if (!url) return null;
  return (
    <div className="chat-output-image">
      <button type="button" className="attach-image" title={`${file.name} · preview`} onClick={() => preview?.show(outputPreview(sessionId, file))}>
        <img src={url} alt={file.name} />
      </button>
    </div>
  );
}

/** One file's badge, name, details and buttons. `change` says what happened to it, in the transcript. */
function OutputDetails({ file, change }: { file: OutputFile; change?: OutputChange["change"] }) {
  const outputs = useOutputs();
  const preview = usePreview();
  const [error, setError] = useState<string | null>(null);
  const kind = kindOf(file.name);
  if (!outputs) return null;
  const { sessionId, saveAs } = outputs;

  async function open() {
    setError(null);
    try {
      await api.openOutput(sessionId, file.name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const time = new Date(file.modified * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <>
      <span className={`attach-badge kind-${kind ?? "text"}`}>{kind ? BADGES[kind] : "FILE"}</span>
      <span className="output-name">{file.name}</span>
      {/* Size and time are the latest version's, so the transcript says only what was done. */}
      <span className="muted output-details">{change ?? `${sizeText(file.size)} · ${time}`}</span>
      {error && <span className="attach-error">{error}</span>}
      <span className="output-actions">
        {preview && (
          <button
            type="button"
            className="button subtle"
            onClick={() => preview.show(outputPreview(sessionId, file))}
            title="Show it in the preview pane"
          >
            Preview
          </button>
        )}
        <button type="button" className="button subtle" onClick={() => void open()} title="Open with its app (scripts are shown in Explorer)">
          Open
        </button>
        <button type="button" className="button subtle" onClick={() => saveAs(file)}>
          Save as…
        </button>
      </span>
    </>
  );
}

/** Save as…: a folder from the hub's own folder picker plus a file name. The desktop app has no native save
 * dialog, and the backend runs on this PC, so it makes the copy. */
function SaveAsDialog({ sessionId, file, onClose }: { sessionId: string; file: OutputFile; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [folder, setFolder] = useState("");
  const [name, setName] = useState(file.name.split("/").at(-1) ?? file.name);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!folder.trim() || !name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const separator = /[\\/]$/.test(folder.trim()) ? "" : "\\";
      const result = await api.saveOutput(sessionId, file.name, `${folder.trim()}${separator}${name.trim()}`);
      setSaved(result.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose}>
      <form onSubmit={submit}>
        <h2>Save a copy</h2>
        <p className="muted small">{file.name}</p>
        <div className="field">
          <span>Folder</span>
          <FolderPicker value={folder} onChange={setFolder} recent={[]} />
        </div>
        <label>
          File name
          <input value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} />
        </label>
        {saved && <div className="hint">Saved to {saved}</div>}
        {error && <div className="composer-error">{error}</div>}
        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            {saved ? "Close" : "Cancel"}
          </button>
          <button type="submit" className="button primary" disabled={busy || !folder.trim() || !name.trim()}>
            {saved ? "Save again" : "Save"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
