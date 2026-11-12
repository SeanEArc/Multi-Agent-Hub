import { useCallback, useEffect, useState } from "react";
import {
  EFFORTS,
  MODELS,
  api,
  type AgentKind,
  type CatalogAgent,
  type LocalModel,
  type LocalModels,
  type ModelCatalog,
  type ModelOption,
} from "../api";

/** The models installed in Ollama, looked up once while `enabled` (null until they arrive). */
export function useLocalModels(enabled: boolean): LocalModels | null {
  const [local, setLocal] = useState<LocalModels | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let current = true;
    setLocal(null);
    api
      .localModels()
      .then((models) => current && setLocal(models))
      .catch(
        (err) =>
          current &&
          setLocal({
            url: "",
            available: false,
            models: [],
            default: null,
            error: err instanceof Error ? err.message : String(err),
          }),
      );
    return () => {
      current = false;
    };
  }, [enabled]);
  return local;
}

/** Why the local model can't be used right now, or null when it can (or the list is still loading). */
export function localProblem(local: LocalModels | null): string | null {
  if (!local || (local.available && local.default)) return null;
  return local.error ?? "No local model is available.";
}

// --- The Claude and Codex model catalog, shared by every mounted picker -------------------------

let catalogCache: ModelCatalog | null = null;
let catalogFailed = false;
let catalogLoading: Promise<void> | null = null;
const catalogListeners = new Set<() => void>();

function notifyCatalog() {
  for (const listener of catalogListeners) listener();
}

/** Replaces the shared catalog (such as with a "Check models" result), updating every picker. */
export function setModelCatalog(catalog: ModelCatalog) {
  catalogCache = catalog;
  catalogFailed = false;
  notifyCatalog();
}

function loadCatalog(force = false): Promise<void> {
  if (catalogLoading) return catalogLoading;
  if (catalogCache && !force) return Promise.resolve();
  catalogLoading = api
    .models()
    .then(
      (catalog) => {
        catalogCache = catalog;
        catalogFailed = false;
      },
      () => {
        // Keep an earlier list if there is one; otherwise the pickers fall back to MODELS.
        catalogFailed = true;
      },
    )
    .finally(() => {
      catalogLoading = null;
      notifyCatalog();
    });
  return catalogLoading;
}

/** The offline list, for when GET /api/models fails. */
function fallbackCatalog(): ModelCatalog {
  const options = (agent: CatalogAgent, group: ModelOption["group"]): ModelOption[] =>
    (MODELS[agent] ?? []).map((id) => ({
      id,
      label: id,
      group,
      available: null,
      note: null,
      checked_at: null,
      efforts: [...EFFORTS],
      default_effort: null,
    }));
  return { claude: options("claude", "version"), codex: options("codex", "version"), codex_source: "builtin", checked_at: {} };
}

export interface ModelCatalogState {
  // The backend's catalog, or the offline list when it couldn't be fetched (null while loading).
  catalog: ModelCatalog | null;
  // True when the offline list is shown because GET /api/models failed.
  offline: boolean;
  refresh: () => Promise<void>;
  setCatalog: (catalog: ModelCatalog) => void;
}

/** The Claude and Codex models, fetched once and shared by every picker. */
export function useModelCatalog(): ModelCatalogState {
  const [, setVersion] = useState(0);
  useEffect(() => {
    const listener = () => setVersion((v) => v + 1);
    catalogListeners.add(listener);
    void loadCatalog();
    return () => {
      catalogListeners.delete(listener);
    };
  }, []);
  const refresh = useCallback(() => loadCatalog(true), []);
  const offline = !catalogCache && catalogFailed;
  return {
    catalog: catalogCache ?? (catalogFailed ? fallbackCatalog() : null),
    offline,
    refresh,
    setCatalog: setModelCatalog,
  };
}

function catalogOptions(agent: AgentKind, catalog: ModelCatalog | null): ModelOption[] {
  if (agent !== "claude" && agent !== "codex") return [];
  return (catalog ?? catalogCache ?? fallbackCatalog())[agent] ?? [];
}

// Which groups come first, and their headings, per agent.
const GROUPS: Record<CatalogAgent, { group: ModelOption["group"]; label: string }[]> = {
  claude: [
    { group: "latest", label: "Latest" },
    { group: "version", label: "Specific versions" },
    { group: "account", label: "Your models" },
  ],
  codex: [
    { group: "latest", label: "Latest" },
    { group: "account", label: "Your Codex models" },
    { group: "version", label: "Built-in list" },
  ],
};

/** Claude's or Codex's models in picker order (by GROUPS, then any others). */
export function orderedModels(agent: CatalogAgent, catalog: ModelCatalog | null): ModelOption[] {
  const options = catalogOptions(agent, catalog);
  const groups = GROUPS[agent];
  return [
    ...groups.flatMap(({ group }) => options.filter((m) => m.group === group)),
    ...options.filter((m) => !groups.some((g) => g.group === m.group)),
  ];
}

interface Props {
  agent: AgentKind;
  value: string | null;
  onChange: (model: string | null) => void;
  // The installed local models, for the local agent (from useLocalModels).
  local: LocalModels | null;
  disabled?: boolean;
  // Leave out the "Default" choice (the CLI's own default model), so a named model must be picked.
  noDefault?: boolean;
  "aria-label"?: string;
}

/**
 * The first named model for the agent: for Claude and Codex the first one the plan isn't known to refuse
 * (Claude's latest aliases first, Codex's account models first), for the local agent the default or first installed.
 */
export function firstModel(agent: AgentKind, local: LocalModels | null, catalog: ModelCatalog | null = null): string | null {
  if (agent === "ollama") return local?.default ?? local?.models[0]?.name ?? null;
  if (agent !== "claude" && agent !== "codex") return null;
  const options = catalogOptions(agent, catalog);
  for (const { group } of GROUPS[agent]) {
    const found = options.find((m) => m.group === group && m.available !== false);
    if (found) return found.id;
  }
  return options.find((m) => m.available !== false)?.id ?? MODELS[agent]?.[0] ?? null;
}

// The value of the "Other…" choice; can't clash with a model name.
const OTHER = "\u0000other";

/** A model picker: the CLI's models for Claude and Codex, the installed models for the local agent. */
export function ModelSelect({ agent, value, onChange, local, disabled, noDefault, "aria-label": ariaLabel }: Props) {
  const { catalog } = useModelCatalog();

  if (agent === "ollama") {
    const installed = local?.models ?? [];
    // A chosen model that isn't installed (any more) stays visible, so the choice isn't silently lost.
    const missing = value && local?.available && !installed.some((m) => m.name === value);
    return (
      <select
        aria-label={ariaLabel}
        value={value ?? ""}
        disabled={disabled || !local?.available}
        onChange={(e) => onChange(e.target.value || null)}
      >
        {!noDefault && <option value="">{local?.default ? `Default (${local.default})` : "Default"}</option>}
        {missing && <option value={value}>{value} (not installed)</option>}
        {installed.map((m) => (
          <option key={m.name} value={m.name}>
            {localModelLabel(m)}
          </option>
        ))}
      </select>
    );
  }

  const options = catalogOptions(agent, catalog);
  const groups = agent === "claude" || agent === "codex" ? GROUPS[agent] : [];
  // A chosen model that isn't listed (typed in, or an old one) stays visible, so the choice isn't lost.
  const custom = value && !options.some((m) => m.id === value) ? value : null;

  function choose(next: string) {
    if (next !== OTHER) {
      onChange(next || null);
      return;
    }
    const name = window.prompt("Model name to pass to the CLI's --model:", custom ?? "")?.trim();
    // Cancelled or empty: the select snaps back to the current value.
    if (name) onChange(name);
  }

  const renderOption = (m: ModelOption) => {
    const refused = m.available === false;
    const selected = m.id === value;
    return (
      <option key={m.id} value={m.id} disabled={refused && !selected} title={m.note ?? undefined}>
        {m.label}
        {refused ? " (not on your plan)" : ""}
      </option>
    );
  };

  return (
    <select aria-label={ariaLabel} value={value ?? ""} disabled={disabled} onChange={(e) => choose(e.target.value)}>
      {(!noDefault || !value) && (
        <option value="" disabled={noDefault}>
          {noDefault ? "Pick a model" : "Default"}
        </option>
      )}
      {custom && <option value={custom}>{custom} (custom)</option>}
      {groups.map(({ group, label }) => {
        const members = options.filter((m) => m.group === group);
        return members.length ? (
          <optgroup key={group} label={label}>
            {members.map(renderOption)}
          </optgroup>
        ) : null;
      })}
      {options.filter((m) => !groups.some((g) => g.group === m.group)).map(renderOption)}
      <option value={OTHER}>Other…</option>
    </select>
  );
}

/** "qwen3.5:9b-q4_K_M (9.0B)", marked when the model can't use tools. */
export function localModelLabel(m: LocalModel): string {
  const size = m.parameter_size ? ` (${m.parameter_size})` : "";
  return `${m.name}${size}${m.tools === false ? ", no tools" : ""}`;
}
