// The pickers on the composer's bottom row: the model (by Send), folder, and auto handoff.
import { useEffect, useRef, useState } from "react";
import { AGENT_NAMES, EFFORTS, EFFORT_NAMES, type AgentKind, type CatalogAgent, type Effort, type LocalModels } from "../api";
import { agentClass } from "./AgentBadge";
import { AgentLogo } from "./AgentLogo";
import { folderName } from "./Dashboard";
import { localModelLabel, orderedModels, useLocalModels, useModelCatalog } from "./ModelSelect";

export const AGENT_OPTIONS: { agent: AgentKind; label: string }[] = [
  { agent: "claude", label: AGENT_NAMES.claude },
  { agent: "codex", label: AGENT_NAMES.codex },
  { agent: "ollama", label: "Local model (Ollama)" },
];

interface ModelPickerProps {
  agent: AgentKind;
  model: string | null;
  // null only for the local model (no effort setting) or a session from before efforts.
  effort: Effort | null;
  // Picking a model of another agent switches the agent too.
  onChange: (agent: AgentKind, model: string | null, effort: Effort | null) => void;
  // The installed local models (useLocalModels), for showing the local agent's default.
  local: LocalModels | null;
  disabled?: boolean;
  title?: string;
}

const PROVIDERS: { agent: AgentKind; name: string }[] = [
  { agent: "claude", name: "Claude" },
  { agent: "codex", name: "Codex (ChatGPT)" },
  { agent: "ollama", name: "Local models (Ollama)" },
];

// One row of the model column: the model plus the efforts it takes.
interface Choice {
  id: string;
  label: string;
  efforts: Effort[];
  refused?: boolean;
  note?: string | null;
  custom?: boolean;
}

/**
 * One box by Send showing the agent's mark, the model and the effort. It opens a menu of three columns:
 * the providers (Claude, Codex, Local), that provider's models, and the chosen model's reasoning efforts.
 * There is no "Default" effort (the default for new sessions is in Settings). Picking an effort (or a model
 * with none) applies the choice; a click outside or Escape cancels it.
 */
export function ModelPicker({ agent, model, effort, onChange, local, disabled, title }: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  // The provider whose models are shown, and a model picked but not applied yet (waiting for its effort).
  const [provider, setProvider] = useState<AgentKind>(agent);
  const [pending, setPending] = useState<{ agent: AgentKind; model: string | null } | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const { catalog } = useModelCatalog();
  // The local list is looked up when the Local tab opens (or is passed in for the local agent).
  const fetchedLocal = useLocalModels(open && provider === "ollama" && agent !== "ollama");
  const localList = agent === "ollama" ? local : fetchedLocal;

  // Each opening starts on the current choice.
  useEffect(() => {
    if (open) {
      setProvider(agent);
      setPending(null);
    }
  }, [open, agent]);

  // Close on a click outside or Escape.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  // The models of `of`; `keep` is a chosen model that stays listed even when the catalog doesn't have it.
  function choices(of: AgentKind, keep: string | null): Choice[] {
    if (of === "ollama") {
      return (localList?.models ?? []).map((m) => ({ id: m.name, label: localModelLabel(m), efforts: [] }));
    }
    if (of !== "claude" && of !== "codex") return [];
    const listed: Choice[] = orderedModels(of, catalog).map((m) => ({
      id: m.id,
      label: m.label,
      efforts: m.efforts,
      refused: m.available === false,
      note: m.note,
    }));
    if (keep && !listed.some((m) => m.id === keep)) {
      listed.unshift({ id: keep, label: keep, efforts: [...EFFORTS], custom: true });
    }
    return listed;
  }

  function find(of: AgentKind, id: string | null): Choice | undefined {
    return id === null ? undefined : choices(of, id).find((m) => m.id === id);
  }

  function label(): string {
    if (agent === "ollama") return model ?? local?.default ?? "Default";
    if (!model) return "Default";
    return find(agent, model)?.label ?? model;
  }

  function apply(nextAgent: AgentKind, nextModel: string | null, nextEffort: Effort | null) {
    setOpen(false);
    onChange(nextAgent, nextModel, nextEffort);
  }

  // The model whose efforts are shown: the one just picked, else the current one while its tab is shown.
  const target = pending ?? (provider === agent ? { agent, model } : null);
  const targetChoice = target ? find(target.agent, target.model) : undefined;
  const targetEfforts: Effort[] =
    !target || target.agent === "ollama"
      ? []
      : (targetChoice?.efforts ?? (target.model === null ? [...EFFORTS] : []));
  // The effort marked in the third column: the current one, if the shown model takes it.
  const markedEffort = effort && targetEfforts.includes(effort) ? effort : null;

  function pickModel(m: Choice) {
    if (m.efforts.length === 0) apply(provider, m.id, null);
    else setPending({ agent: provider, model: m.id });
  }

  function other(of: CatalogAgent) {
    const current = of === agent ? (model ?? "") : "";
    const name = window.prompt(`${AGENT_NAMES[of]} model name to pass to the CLI's --model:`, current)?.trim();
    if (name) setPending({ agent: of, model: name });
  }

  const shown = label();
  const shownEffort = effort ? EFFORT_NAMES[effort] : null;
  const keep = pending?.agent === provider ? pending.model : provider === agent ? model : null;
  const models = choices(provider, keep);
  const selectedId = target && target.agent === provider ? target.model : null;
  const localDefault = provider === "ollama" && agent === "ollama" && !pending && !model ? local?.default : null;

  return (
    <div className="model-picker" ref={box}>
      <button
        type="button"
        className={`composer-picker model-button${open ? " open" : ""}`}
        onClick={() => setOpen(!open)}
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={title ?? `${AGENT_NAMES[agent] ?? agent}: ${shown}${shownEffort ? `, ${shownEffort} effort` : ""}`}
      >
        <AgentLogo agent={agent} />
        <span className="model-button-name">{shown}</span>
        {shownEffort && <span className="model-button-effort">{shownEffort}</span>}
        <span className="muted" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <div className={`model-menu ${agentClass(provider)}`} role="dialog" aria-label="Model and effort">
          <div className="model-menu-providers" role="tablist" aria-label="Provider">
            {PROVIDERS.map((p) => (
              <button
                key={p.agent}
                type="button"
                role="tab"
                aria-selected={provider === p.agent}
                aria-label={p.name}
                className={`model-menu-provider ${agentClass(p.agent)}${provider === p.agent ? " selected" : ""}`}
                title={p.name}
                onClick={() => setProvider(p.agent)}
              >
                <AgentLogo agent={p.agent} />
              </button>
            ))}
          </div>

          <div className="model-menu-column" role="listbox" aria-label="Model">
            <div className="model-menu-heading">{PROVIDERS.find((p) => p.agent === provider)?.name}</div>
            {provider === "ollama" && localList === null && <span className="model-menu-note muted">Looking for Ollama…</span>}
            {provider === "ollama" && localList && (!localList.available || localList.models.length === 0) && (
              <span className="model-menu-note muted">{localList.error ?? "No local models installed."}</span>
            )}
            {models.map((m) => {
              const selected = m.id === selectedId || m.id === localDefault;
              return (
                <button
                  key={m.id}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  className={`model-menu-item${selected ? " selected" : ""}`}
                  disabled={m.refused && !selected}
                  title={m.note ?? undefined}
                  onClick={() => pickModel(m)}
                >
                  <span className="model-menu-label">
                    {m.label}
                    {m.custom && <span className="muted"> (custom)</span>}
                    {m.refused && <span className="muted"> (not on your plan)</span>}
                  </span>
                  {m.efforts.length > 0 && (
                    <span className="model-menu-more muted" aria-hidden="true">
                      ›
                    </span>
                  )}
                </button>
              );
            })}
            {(provider === "claude" || provider === "codex") && (
              <button type="button" className="model-menu-item muted" onClick={() => other(provider)}>
                Other…
              </button>
            )}
          </div>

          <div className="model-menu-column" role="listbox" aria-label="Reasoning effort">
            <div className="model-menu-heading">Effort</div>
            {!target ? (
              <span className="model-menu-note muted">Pick a model.</span>
            ) : targetEfforts.length === 0 ? (
              <span className="model-menu-note muted">
                {target.agent === "ollama" ? "Local models have no effort setting." : "This model has no effort setting."}
              </span>
            ) : (
              <>
                {targetEfforts.map((e) => (
                  <button
                    key={e}
                    type="button"
                    role="option"
                    aria-selected={markedEffort === e}
                    className={`model-menu-item${markedEffort === e ? " selected" : ""}`}
                    onClick={() => apply(target.agent, target.model, e)}
                  >
                    {EFFORT_NAMES[e]}
                  </button>
                ))}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

interface FolderButtonProps {
  path: string;
  open: boolean;
  onToggle: () => void;
  worktree: boolean;
}

/** The folder a new chat starts in; opens the folder picker below the text box. */
export function FolderButton({ path, open, onToggle, worktree }: FolderButtonProps) {
  const folder = path.trim();
  return (
    <button
      type="button"
      className={`composer-picker folder-button${open ? " open" : ""}`}
      onClick={onToggle}
      aria-expanded={open}
      title={folder ? `${folder}${worktree ? " (in a separate git worktree)" : ""}` : "No folder: the agent works in an empty folder of its own"}
    >
      <span aria-hidden="true">📁</span>
      <span className="folder-button-name">{folder ? folderName(folder) : "Chat, no folder"}</span>
      {folder && worktree && <span className="muted">⑂</span>}
      <span className="muted" aria-hidden="true">
        ▾
      </span>
    </button>
  );
}

/** The folder an existing session runs in, which can't change. */
export function FolderLock({ chat, path }: { chat: boolean; path: string }) {
  return (
    <span
      className="composer-picker folder-button locked"
      title={`${chat ? "Chat, no project folder" : path}. A conversation can only resume from the folder it began in, so the folder can't change; start a new session for another folder.`}
    >
      <span aria-hidden="true">🔒</span>
      <span className="folder-button-name">{chat ? "Chat" : folderName(path)}</span>
    </span>
  );
}

interface AutoHandoffProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}

/** Whether the next agent in the chain takes over at once when usage runs out, or the hub asks first. */
export function AutoHandoffSwitch({ checked, onChange, disabled }: AutoHandoffProps) {
  return (
    <label
      className="composer-picker composer-toggle"
      title={
        checked
          ? "On: when the agent runs out of usage, the next agent in the chain takes over at once."
          : "Off: when the agent runs out of usage, the hub offers the next agent in the chain and waits for you."
      }
    >
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle-track" aria-hidden="true" />
      Auto handoff
    </label>
  );
}

/** A chat's title when none is given: the start of its first message. */
export function chatTitle(firstMessage: string): string {
  const line = firstMessage.trim().split("\n")[0];
  if (!line) return "Chat";
  return line.length > 60 ? `${line.slice(0, 57)}…` : line;
}
