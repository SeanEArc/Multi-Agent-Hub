import { useRef, useState } from "react";
import { api, ApiError, type FolderListing } from "../api";

interface Props {
  value: string;
  onChange: (path: string) => void;
  // Folders used by earlier sessions, newest first.
  recent: string[];
}

// The browser can't hand us real paths, so the backend lists folders and the
// picker walks them. The folder being viewed is the selection.
export function FolderPicker({ value, onChange, recent }: Props) {
  const [browsing, setBrowsing] = useState(false);
  const [listing, setListing] = useState<FolderListing | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestId = useRef(0);

  async function open(path: string | null, hidden = showHidden) {
    const id = ++requestId.current;
    setLoading(true);
    setError(null);
    try {
      const result = await api.listFolders(path, hidden);
      if (id !== requestId.current) return;
      setListing(result);
      if (result.path) onChange(result.path);
    } catch (err) {
      if (id !== requestId.current) return;
      // A typed path that doesn't exist: start from the top instead.
      if (err instanceof ApiError && err.status === 404 && path) {
        await open(null, hidden);
        return;
      }
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (id === requestId.current) setLoading(false);
    }
  }

  function toggleBrowse() {
    if (browsing) {
      requestId.current++; // ignore a listing still in flight
      setLoading(false);
      setBrowsing(false);
      return;
    }
    setBrowsing(true);
    void open(value.trim() || null);
  }

  const shownRecent = recent.filter((path) => path !== value.trim()).slice(0, 5);

  return (
    <div className="folder-picker">
      <div className="folder-input-row">
        <input
          id="project-path"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required
          placeholder="C:\Users\you\projects\my-app"
          spellCheck={false}
        />
        <button type="button" className="button" onClick={toggleBrowse} aria-expanded={browsing}>
          {browsing ? "Done" : "Browse…"}
        </button>
      </div>

      {browsing && (
        <div className="folder-browser">
          <div className="folder-browser-header">
            <button
              type="button"
              className="button subtle"
              disabled={loading || !listing?.path}
              onClick={() => void open(listing?.parent ?? null)}
              title="Up one folder"
            >
              ↑ Up
            </button>
            <span className="path" title={listing?.path ?? undefined}>
              {listing?.path ?? "This PC"}
            </span>
            <label className="folder-hidden-toggle">
              <input
                type="checkbox"
                checked={showHidden}
                onChange={(e) => {
                  setShowHidden(e.target.checked);
                  void open(listing?.path ?? null, e.target.checked);
                }}
              />
              Hidden
            </label>
          </div>
          <div className="folder-list" role="listbox" aria-label="Folders" aria-busy={loading}>
            {error && <div className="composer-error">{error}</div>}
            {!error && listing?.folders.length === 0 && <div className="muted small">No subfolders.</div>}
            {listing?.folders.map((folder) => (
              <button
                type="button"
                key={folder.path}
                className="folder-item"
                onClick={() => void open(folder.path)}
                title={folder.path}
              >
                <span aria-hidden>📁</span>
                <span className="folder-item-name">{folder.name}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {!browsing && shownRecent.length > 0 && (
        <div className="recent-folders">
          <span className="hint">Recent:</span>
          {shownRecent.map((path) => (
            <button type="button" key={path} className="chip recent-folder" onClick={() => onChange(path)} title={path}>
              {folderName(path)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function folderName(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, "");
  return trimmed.split(/[\\/]/).at(-1) || path;
}
