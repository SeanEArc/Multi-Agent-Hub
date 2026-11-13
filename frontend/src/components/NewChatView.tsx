import { useEffect, useRef, useState } from "react";
import { api, type AgentKind, type Effort, type GitInfo, type HubSettings, type LimitPolicy } from "../api";
import { agentClass } from "./AgentBadge";
import { Composer } from "./Composer";
import { AutoHandoffSwitch, FolderButton, ModelPicker, chatTitle } from "./ComposerControls";
import { FolderPicker } from "./FolderPicker";
import { localProblem, useLocalModels } from "./ModelSelect";

interface Props {
  // null until loaded; the pickers start from its defaults.
  settings: HubSettings | null;
  // Project folders of existing sessions, newest first, offered as shortcuts.
  recentProjects: string[];
  // The session exists and has its first message; show it.
  onCreated: (sessionId: string) => Promise<void>;
}

/** A new session before its first message: nothing is created until the user sends one. */
export function NewChatView({ settings, recentProjects, onCreated }: Props) {
  const [agent, setAgent] = useState<AgentKind>(settings?.default_agent ?? "claude");
  const [model, setModel] = useState<string | null>(settings?.default_model ?? null);
  const [effort, setEffort] = useState<Effort | null>(settings?.default_effort ?? "medium");
  const [projectPath, setProjectPath] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [useWorktree, setUseWorktree] = useState(true);
  const [git, setGit] = useState<GitInfo | null>(null);
  // null: follow the Settings default.
  const [autoHandoff, setAutoHandoff] = useState<boolean | null>(null);
  // Created, but its first message failed: a retry sends to it rather than creating another.
  const created = useRef<string | null>(null);

  // Start from the Settings defaults once they arrive, unless the user already picked.
  const picked = useRef(false);
  useEffect(() => {
    if (!settings || picked.current) return;
    setAgent(settings.default_agent ?? "claude");
    setModel(settings.default_model ?? null);
    setEffort(settings.default_effort ?? "medium");
  }, [settings]);

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

  const local = useLocalModels(agent === "ollama");
  const problem = agent === "ollama" ? localProblem(local) : null;
  // The repository's name when the folder is in one, else the folder's own name.
  const folderName = baseName(git?.is_repo && git.root ? git.root : projectPath.trim());
  const canUseWorktree = !!git?.is_repo && git.has_commits;
  const worktree = canUseWorktree && useWorktree;
  const chaining = settings?.chaining_enabled ?? true;
  const defaultPolicy: LimitPolicy = settings?.limit_policy ?? "auto";
  const auto = autoHandoff ?? defaultPolicy === "auto";

  async function send(text: string) {
    let id = created.current;
    if (!id) {
      const policy: LimitPolicy = auto ? "auto" : "ask";
      const session = await api.createSession({
        title: chatTitle(text),
        project_path: projectPath.trim() || null,
        agent,
        model,
        effort: agent === "ollama" ? null : effort,
        chain: null,
        limit_policy: !chaining || policy === defaultPolicy ? null : policy,
        use_worktree: worktree,
      });
      id = created.current = session.id;
    }
    try {
      await api.sendMessage(id, text);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      throw new Error(`The session was created, but the message wasn't sent: ${message}`);
    }
    await onCreated(id);
  }

  const notes = (
    <>
      {agent === "ollama" && (
        <span className={`hint${problem ? " warning" : ""}`}>
          {local === null
            ? "Looking for Ollama…"
            : problem ??
              "Runs on this PC with no subscription. Local models are slower and weaker at coding than Claude and Codex."}
        </span>
      )}
      {worktree && git && git.changes.length > 0 && (
        <span className="hint warning">
          Your {git.changes.length} uncommitted {git.changes.length === 1 ? "change is" : "changes are"} not copied into
          the worktree. Commit them first if the agent needs them.
        </span>
      )}
    </>
  );
  const hasNotes = agent === "ollama" || (worktree && !!git && git.changes.length > 0);

  const panel = browsing && (
    <div className="folder-panel">
      <FolderPicker value={projectPath} onChange={setProjectPath} recent={recentProjects} />
      <div className="folder-panel-footer">
        <span className="hint">
          {projectPath.trim()
            ? "The agent can edit files in this folder."
            : "Leave it empty to just chat. The agent works in an empty folder of its own, deleted with the session."}
        </span>
        {projectPath.trim() && (
          <button type="button" className="button subtle" onClick={() => setProjectPath("")}>
            No folder
          </button>
        )}
        <button type="button" className="button" onClick={() => setBrowsing(false)}>
          Done
        </button>
      </div>
      {git?.is_repo && (
        <div className="folder-panel-worktree">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={worktree}
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
        </div>
      )}
    </div>
  );

  return (
    <section className={`session-view ${agentClass(agent)}`}>
      <header className="session-header">
        <div className="session-heading">
          <h1>New session</h1>
          <div className="session-meta">
            <span className="muted small">Pick the agent, model and folder under the text box, then send your first message.</span>
          </div>
        </div>
      </header>
      <div className="session-body panel">
        <div className="session-main">
          <div className="new-chat-center">
            <h2 className="new-chat-prompt">
              {folderName ? `What should we build in ${folderName}?` : "What should we build today?"}
            </h2>
            <Composer
              status="ready"
              agent={agent}
              loadCommands={async () => (await api.commands(agent, { path: projectPath.trim() || undefined })).commands}
              commandsKey={`${agent}:${projectPath.trim()}`}
              placeholder={projectPath.trim() ? "Describe the task…" : "Ask anything…"}
              onSend={send}
              onStop={async () => undefined}
              sendBlocked={!!problem || (agent === "ollama" && local === null)}
              panel={panel || undefined}
              notes={hasNotes ? notes : undefined}
              picker={
                <ModelPicker
                  agent={agent}
                  model={model}
                  effort={effort}
                  local={local}
                  onChange={(a, m, e) => {
                    picked.current = true;
                    setAgent(a);
                    setModel(m);
                    setEffort(e);
                  }}
                />
              }
              controls={
                <>
                  <FolderButton path={projectPath} open={browsing} onToggle={() => setBrowsing(!browsing)} worktree={worktree} />
                  {chaining && <AutoHandoffSwitch checked={auto} onChange={setAutoHandoff} />}
                </>
              }
            />
          </div>
        </div>
      </div>
    </section>
  );
}

function baseName(path: string): string {
  const parts = path.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] ?? "";
}
