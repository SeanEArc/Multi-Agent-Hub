import { useEffect, useRef, useState, type FormEvent } from "react";
import { MODELS, api, type AgentKind, type ChainEntry, type GitInfo, type LimitPolicy, type NewSession } from "../api";
import { ChainEditor, POLICY_LABELS } from "./ChainEditor";
import { FolderPicker } from "./FolderPicker";

interface Props {
  onCreate: (session: NewSession, firstMessage: string) => Promise<void>;
  onClose: () => void;
  // Project folders of existing sessions, newest first, offered as shortcuts.
  recentProjects: string[];
}

const ALL_AGENTS: AgentKind[] = ["claude", "codex", "ollama"];

export function NewSessionDialog({ onCreate, onClose, recentProjects }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState("");
  const [projectPath, setProjectPath] = useState("");
  const [agent, setAgent] = useState<AgentKind>("claude");
  const [model, setModel] = useState("");
  const [firstMessage, setFirstMessage] = useState("");
  // null: use the defaults from Settings.
  const [chain, setChain] = useState<ChainEntry[] | null>(null);
  const [policy, setPolicy] = useState<LimitPolicy | "">("");
  const [useWorktree, setUseWorktree] = useState(true);
  const [git, setGit] = useState<GitInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    dialog.current?.showModal();
  }, []);

  // Look up the folder's repository once typing pauses, to offer a worktree.
  useEffect(() => {
    const path = projectPath.trim();
    setGit(null);
    if (!path) return;
    let current = true;
    const timer = setTimeout(() => {
      api
        .gitInfo(path)
        .then((info) => current && setGit(info))
        .catch(() => current && setGit(null));
    }, 300);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [projectPath]);

  const canUseWorktree = !!git?.is_repo && git.has_commits;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onCreate(
        {
          title: title.trim(),
          project_path: projectPath.trim(),
          agent,
          model: model || null,
          chain,
          limit_policy: policy || null,
          use_worktree: canUseWorktree && useWorktree,
        },
        firstMessage.trim(),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} className="dialog" onClose={onClose}>
      <form onSubmit={submit}>
        <h2>New session</h2>

        <label>
          Title
          <input value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus placeholder="Fix login bug" />
        </label>

        <div className="field">
          <label htmlFor="project-path">Project folder</label>
          <FolderPicker value={projectPath} onChange={setProjectPath} recent={recentProjects} />
          <span className="hint">Type or paste a path, or browse to the folder. The agent can edit files in it.</span>
        </div>

        {git?.is_repo && (
          <div className="field">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={canUseWorktree && useWorktree}
                disabled={!canUseWorktree}
                onChange={(e) => setUseWorktree(e.target.checked)}
              />
              Work in a separate git worktree
            </label>
            <span className="hint">
              {!git.has_commits
                ? "The repository has no commits yet, so the session runs in the folder itself."
                : useWorktree
                  ? `The agents work on a new branch in their own folder, starting from ${git.branch ?? "the current commit"}. Other sessions and your own checkout are not affected.`
                  : "The agents work directly in this folder, on your current branch."}
            </span>
            {canUseWorktree && useWorktree && git.changes.length > 0 && (
              <span className="hint warning">
                Your {git.changes.length} uncommitted {git.changes.length === 1 ? "change is" : "changes are"} not
                copied into the worktree. Commit them first if the agent needs them.
              </span>
            )}
          </div>
        )}

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
              <option value="claude">Claude Code</option>
              <option value="codex">Codex</option>
              <option value="ollama" disabled>
                Local model (phase 5)
              </option>
            </select>
          </label>
          <label>
            Model
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {(MODELS[agent] ?? [""]).map((m) => (
                <option key={m} value={m}>
                  {m || "Default"}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="field">
          <span>Handoff</span>
          <div className="field-row">
            <label>
              Chain
              <select
                value={chain ? "custom" : "default"}
                onChange={(e) =>
                  setChain(
                    e.target.value === "custom"
                      ? [{ agent, model: model || null }, ...ALL_AGENTS.filter((a) => a !== agent).map((a) => ({ agent: a, model: null }))]
                      : null,
                  )
                }
              >
                <option value="default">Default (from Settings)</option>
                <option value="custom">Custom for this session</option>
              </select>
            </label>
            <label>
              On a usage limit
              <select value={policy} onChange={(e) => setPolicy(e.target.value as LimitPolicy | "")}>
                <option value="">Default (from Settings)</option>
                {Object.entries(POLICY_LABELS).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {chain && <ChainEditor chain={chain} onChange={setChain} />}
        </div>

        <label>
          <span>
            First message <span className="hint">(optional)</span>
          </span>
          <textarea
            value={firstMessage}
            onChange={(e) => setFirstMessage(e.target.value)}
            rows={4}
            placeholder="Describe the task. You can also send it later."
          />
        </label>

        {error && <div className="composer-error">{error}</div>}

        <div className="dialog-actions">
          <button type="button" className="button" onClick={() => dialog.current?.close()}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={busy}>
            {firstMessage.trim() ? "Create and start" : "Create"}
          </button>
        </div>
      </form>
    </dialog>
  );
}
