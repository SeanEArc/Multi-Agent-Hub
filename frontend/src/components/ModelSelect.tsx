import { useEffect, useState } from "react";
import { MODELS, api, type AgentKind, type LocalModel, type LocalModels } from "../api";

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

interface Props {
  agent: AgentKind;
  value: string | null;
  onChange: (model: string | null) => void;
  // The installed local models, for the local agent (from useLocalModels).
  local: LocalModels | null;
  disabled?: boolean;
  "aria-label"?: string;
}

/** A model picker: the CLI's models for Claude and Codex, the installed models for the local agent. */
export function ModelSelect({ agent, value, onChange, local, disabled, "aria-label": ariaLabel }: Props) {
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
        <option value="">{local?.default ? `Default (${local.default})` : "Default"}</option>
        {missing && <option value={value}>{value} (not installed)</option>}
        {installed.map((m) => (
          <option key={m.name} value={m.name}>
            {localModelLabel(m)}
          </option>
        ))}
      </select>
    );
  }
  return (
    <select aria-label={ariaLabel} value={value ?? ""} disabled={disabled} onChange={(e) => onChange(e.target.value || null)}>
      {(MODELS[agent] ?? [""]).map((m) => (
        <option key={m} value={m}>
          {m || "Default"}
        </option>
      ))}
    </select>
  );
}

/** "qwen3.5:9b-q4_K_M (9.0B)", marked when the model can't use tools. */
export function localModelLabel(m: LocalModel): string {
  const size = m.parameter_size ? ` (${m.parameter_size})` : "";
  return `${m.name}${size}${m.tools === false ? ", no tools" : ""}`;
}
