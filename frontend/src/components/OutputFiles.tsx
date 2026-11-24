// Files agents wrote to the session's outputs folder (backend/app/outputs.py): a row each with Preview, Open and
// Save as….
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, type OutputFile } from "../api";
import { kindOf, sizeText } from "../attachments";
import { outputPreview, usePreview } from "../preview";
import { BADGES } from "./Attachments";
import { FolderPicker } from "./FolderPicker";

interface Props {
  sessionId: string;
  // Changes when the list may have changed (a turn ended, a tool finished), to load it again.
  refreshKey: string;
}

/** Above the composer once an agent has written something: the outputs folder's files. */
export function OutputFiles({ sessionId, refreshKey }: Props) {
  const [files, setFiles] = useState<OutputFile[]>([]);
  const [saving, setSaving] = useState<OutputFile | null>(null);
  const preview = usePreview();

  // A file in the preview pane that an agent overwrote (new size or time) is shown again.
  const shown = preview?.current?.outputName;
  useEffect(() => {
    const file = files.find((f) => f.name === shown);
    if (!preview || !file) return;
    const next = outputPreview(sessionId, file);
    if (next.version !== preview.current?.version) preview.show(next);
  }, [files, shown]);

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

  if (!files.length) return null;
  const newestFirst = [...files].sort((a, b) => b.modified - a.modified);
  return (
    <details className="outputs-bar" open>
      <summary>
        Files from agents <span className="muted">· {files.length}</span>
      </summary>
      <ul className="outputs-list">
        {newestFirst.map((file) => (
          <OutputRow
            key={file.name}
            sessionId={sessionId}
            file={file}
            onPreview={preview ? () => preview.show(outputPreview(sessionId, file)) : undefined}
            onSaveAs={() => setSaving(file)}
          />
        ))}
      </ul>
      {saving && <SaveAsDialog sessionId={sessionId} file={saving} onClose={() => setSaving(null)} />}
    </details>
  );
}

interface RowProps {
  sessionId: string;
  file: OutputFile;
  onPreview?: () => void;
  onSaveAs: () => void;
}

function OutputRow({ sessionId, file, onPreview, onSaveAs }: RowProps) {
  const [error, setError] = useState<string | null>(null);
  const kind = kindOf(file.name);

  async function open() {
    setError(null);
    try {
      await api.openOutput(sessionId, file.name);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <li className="output-row" title={file.path}>
      <span className={`attach-badge kind-${kind ?? "text"}`}>{kind ? BADGES[kind] : "FILE"}</span>
      <span className="output-name">{file.name}</span>
      <span className="muted output-details">
        {sizeText(file.size)} · {new Date(file.modified * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
      </span>
      {error && <span className="attach-error">{error}</span>}
      <span className="output-actions">
        {onPreview && (
          <button type="button" className="button subtle" onClick={onPreview} title="Show it in the preview pane">
            Preview
          </button>
        )}
        <button type="button" className="button subtle" onClick={() => void open()} title="Open with its app (scripts are shown in Explorer)">
          Open
        </button>
        <button type="button" className="button subtle" onClick={onSaveAs}>
          Save as…
        </button>
      </span>
    </li>
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
