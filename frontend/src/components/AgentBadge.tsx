import type { ReactNode } from "react";
import { AGENT_NAMES, SHORT_NAMES, type AgentKind } from "../api";

/** The CSS class that sets `--agent` to an agent's color, for it and everything inside it. */
export function agentClass(agent: string): string {
  return `agent-${agent in AGENT_NAMES ? agent : "other"}`;
}

/** A chip with the agent's color dot: "● Codex", or "● Claude (opus)" when `children` adds the model. */
export function AgentChip({ agent, short, children }: { agent: AgentKind; short?: boolean; children?: ReactNode }) {
  return (
    <span className={`chip agent-chip ${agentClass(agent)}`}>
      <span className="agent-dot" aria-hidden="true" />
      {(short ? SHORT_NAMES : AGENT_NAMES)[agent] ?? agent}
      {children}
    </span>
  );
}

/** The agent's name in its color, for running text such as the sidebar and handoff lines. */
export function AgentName({ agent, short }: { agent: string; short?: boolean }) {
  const names: Record<string, string> = short ? SHORT_NAMES : AGENT_NAMES;
  return <span className={`agent-name ${agentClass(agent)}`}>{names[agent] ?? agent}</span>;
}
