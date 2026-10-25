import { useEffect, useState } from "react";
import { AGENT_NAMES, api, type AgentKind, type AgentUsage } from "../api";

const POLL_MS = 60000;
const LABELS: Record<string, string> = { five_hour: "5h", seven_day: "7d" };

interface Props {
  // Changes when the open session saves a new usage report, so the meters update at once.
  refreshKey: number | undefined;
  // The agent working in the open session, shown first.
  current: AgentKind;
}

/** Plan usage for every agent the hub has seen, from each one's latest report in any session. */
export function UsageMeters({ refreshKey, current }: Props) {
  const [usage, setUsage] = useState<Partial<Record<AgentKind, AgentUsage>>>({});

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api.getUsage().then(
        (u) => !cancelled && setUsage(u),
        () => undefined, // the banner already reports a down backend
      );
    void load();
    const timer = setInterval(() => void load(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [refreshKey]);

  const agents = (Object.keys(usage) as AgentKind[]).sort((a, b) => Number(b === current) - Number(a === current));
  if (agents.length === 0) return null;
  return (
    <span className="usage-meters">
      {agents.map((agent) => (
        <UsageMeter key={agent} agent={agent} usage={usage[agent]!} />
      ))}
    </span>
  );
}

function UsageMeter({ agent, usage }: { agent: AgentKind; usage: AgentUsage }) {
  const name = AGENT_NAMES[agent] ?? agent;
  const now = Date.now() / 1000;
  const windows = Object.entries(usage.windows).flatMap(([key, w]) => {
    if (typeof w?.utilization !== "number") return [];
    // Epoch seconds; Claude names it resetsAt, the Codex adapter resets_at.
    const resetsAt: number | undefined = w.resets_at ?? w.resetsAt ?? undefined;
    return [{ key, utilization: w.utilization as number, resetsAt, reset: resetsAt !== undefined && resetsAt < now }];
  });
  const title = [
    `${name}${usage.plan_type ? ` (${usage.plan_type})` : ""}: share of the plan's usage limits used`,
    `Reported ${new Date(usage.reported_at).toLocaleString()}; updates when ${name} runs a turn in the hub`,
    ...windows.map(
      (w) =>
        `${LABELS[w.key] ?? w.key} ${w.reset ? "has reset since" : "resets"} ${w.resetsAt ? new Date(w.resetsAt * 1000).toLocaleString() : "(unknown)"}`,
    ),
  ].join("\n");

  return (
    <span className={`usage-meter${usage.status === "rejected" ? " limit" : ""}`} title={title}>
      <span className="usage-agent">
        {name}
        {usage.plan_type ? ` ${usage.plan_type}` : ""}
      </span>
      {windows.map((w) => (
        <span key={w.key} className={`usage-window${w.reset ? " stale" : ""}`}>
          {LABELS[w.key] ?? w.key}
          <span className="usage-bar">
            <span style={{ width: `${w.reset ? 0 : Math.min(100, w.utilization * 100)}%` }} />
          </span>
          {w.reset ? "reset" : `${Math.round(w.utilization * 100)}%`}
        </span>
      ))}
    </span>
  );
}
