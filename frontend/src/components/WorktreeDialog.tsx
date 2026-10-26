import { useEffect, useRef, useState } from "react";
import { ApiError, api, type AgentSession, type Worktree } from "../api";

interface Props {
  session: AgentSession;
  worktree: Worktree;
  busy: boolean;
  onDone: () => void;
  onClose: () => void;
}

/** The session's worktree: where it is, and removing it once the work is done (the branch is kept to merge). */
export function WorktreeDialog({ session, worktree, busy, onDone, onClose }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  // Uncommitted changes reported when a plain remove was refused.
  const [changes, setChanges] = useState<string[] | null>(null);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = worktree.status === "active";

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  async function remove(options: { commit?: boolean; discard?: boolean } = {}) {
    if (options.discard && !confirm(`Throw away ${changes?.length ?? 0} uncommitted change(s)? This can't be undone.`)) {
      return;
    }
    setWorking(true);
    setError(null);
    try {
      await api.removeWorktree(session.id, options);
      onDone();
      dialog.current?.close();
    } catch (err) {
      if (err instanceof ApiError && Array.isArray(err.detail?.changes)) {
        setChanges(err.detail.changes);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
      setWorking(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose}>
      <div className="dialog-body">
        <h2>Worktree</h2>
        <p className="muted small">
          {active
            ? "This session's agents work on their own branch in a separate folder, so other sessions and your own checkout are not affected."
            : "This session's worktree folder was removed. Its committed work stays on the branch until you merge or delete it."}
        </p>

        <dl className="facts">
          <dt>Branch</dt>
          <dd>
            <code>{worktree.branch}</code>
          </dd>
          <dt>Started from</dt>
          <dd>
            <code>{worktree.base_commit.slice(0, 10)}</code>
          </dd>
          <dt>Repository</dt>
          <dd>
            <code>{worktree.repo_path}</code>
          </dd>
          {active && (
            <>
              <dt>Folder</dt>
              <dd>
                <code>{worktree.path}</code>
              </dd>
            </>
          )}
        </dl>

        {active && (
          <p className="muted small">
            To bring the work into your checkout, merge the branch, for example{" "}
            <code>git merge {worktree.branch}</code>. Removing the worktree deletes its folder and keeps the branch (a
            branch with no commits of its own is deleted too).
          </p>
        )}

        {changes && (
          <div className="worktree-changes">
            <p className="small">The worktree has uncommitted changes. Commit them to the branch, or throw them away:</p>
            <pre>{changes.join("\n")}</pre>
          </div>
        )}

        {error && <div className="composer-error">{error}</div>}

        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            Close
          </button>
          {active &&
            (changes ? (
              <>
                <button className="button danger" disabled={working} onClick={() => void remove({ discard: true })}>
                  Discard and remove
                </button>
                <button className="button primary" disabled={working} onClick={() => void remove({ commit: true })}>
                  Commit and remove
                </button>
              </>
            ) : (
              <button
                className="button primary"
                disabled={working || busy}
                title={busy ? "Stop the running turn first" : undefined}
                onClick={() => void remove()}
              >
                Remove worktree
              </button>
            ))}
        </div>
      </div>
    </dialog>
  );
}
