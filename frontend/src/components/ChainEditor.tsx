import { AGENT_NAMES, AVAILABLE_AGENTS, MODELS, type AgentKind, type ChainEntry, type LimitPolicy } from "../api";

interface Props {
  chain: ChainEntry[];
  onChange: (chain: ChainEntry[]) => void;
}

const AGENTS: AgentKind[] = ["claude", "codex", "ollama"];

/** The handoff order: when an agent hits its usage limit, the next one in the list takes over. */
export function ChainEditor({ chain, onChange }: Props) {
  function update(index: number, entry: ChainEntry) {
    onChange(chain.map((e, i) => (i === index ? entry : e)));
  }

  function move(index: number, by: number) {
    const next = [...chain];
    [next[index], next[index + by]] = [next[index + by], next[index]];
    onChange(next);
  }

  return (
    <ol className="chain-editor">
      {chain.map((entry, i) => {
        const unavailable = !AVAILABLE_AGENTS.includes(entry.agent);
        return (
          <li key={i} className="chain-row">
            <span className="chain-step">{i + 1}</span>
            <select
              aria-label={`Agent ${i + 1}`}
              value={entry.agent}
              onChange={(e) => update(i, { agent: e.target.value as AgentKind, model: null })}
            >
              {AGENTS.map((agent) => (
                <option key={agent} value={agent} disabled={!AVAILABLE_AGENTS.includes(agent)}>
                  {AGENT_NAMES[agent]}
                  {AVAILABLE_AGENTS.includes(agent) ? "" : " (phase 5)"}
                </option>
              ))}
            </select>
            <select
              aria-label={`Model ${i + 1}`}
              value={entry.model ?? ""}
              disabled={unavailable}
              onChange={(e) => update(i, { ...entry, model: e.target.value || null })}
            >
              {(MODELS[entry.agent] ?? [""]).map((m) => (
                <option key={m} value={m}>
                  {m || "Default"}
                </option>
              ))}
            </select>
            <span className="chain-buttons">
              <button type="button" className="button subtle" onClick={() => move(i, -1)} disabled={i === 0} title="Move up">
                ↑
              </button>
              <button
                type="button"
                className="button subtle"
                onClick={() => move(i, 1)}
                disabled={i === chain.length - 1}
                title="Move down"
              >
                ↓
              </button>
              <button
                type="button"
                className="button subtle"
                onClick={() => onChange(chain.filter((_, j) => j !== i))}
                disabled={chain.length === 1}
                title="Remove"
              >
                ×
              </button>
            </span>
            {unavailable && <span className="hint chain-note">Skipped until it's available</span>}
          </li>
        );
      })}
      <li>
        <button
          type="button"
          className="button subtle"
          onClick={() => onChange([...chain, { agent: "codex", model: null }])}
        >
          + Add agent
        </button>
      </li>
    </ol>
  );
}

export const POLICY_LABELS: Record<LimitPolicy, string> = {
  auto: "Hand off automatically",
  ask: "Ask me first",
};
