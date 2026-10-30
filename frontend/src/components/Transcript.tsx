import { Fragment, useState, type ReactNode } from "react";
import { MarkdownText } from "./MarkdownText";
import { AGENT_NAMES, api, type AgentKind, type SessionEvent, type SessionStatus } from "../api";
import { formatTokens, turnTokens } from "../history";
import { isSubagent, planCounts, stepsByParent, toolOutcomes, type PlanItem, type ToolOutcomes } from "../progress";

interface Props {
  sessionId: string;
  events: SessionEvent[];
  status: SessionStatus;
  projectPath: string;
  // Confirms a handoff the hub offered after a usage limit (the "ask first" policy).
  onAcceptHandoff: (agent: AgentKind, model: string | null) => Promise<void>;
  // Shown at the top of the transcript (the progress card).
  header?: ReactNode;
}

export const REASONS: Record<string, string> = {
  limit: "usage limit",
  manual: "handed off by you",
  delegation: "delegated",
};

// Events that end a turn. The plan as it stood then is shown in the transcript.
const TURN_ENDS = ["completed", "question", "error", "limit_reached", "user_message", "handoff"];

/**
 * Renders a session's events as a conversation, pairing each tool call with its result.
 * A subagent's steps are folded under the tool call that started it.
 */
export function Transcript({ sessionId, events, status, projectPath, onAcceptHandoff, header }: Props) {
  const outcomes = toolOutcomes(events);
  const { results, denied } = outcomes;
  const steps = stepsByParent(events);
  const running = status === "working";

  // Each turn's plan is shown once, as it stood when the turn ended; the running turn's is in the progress panel.
  const finalPlans = new Set<number>();
  let latestPlan: SessionEvent | null = null;
  for (const event of events) {
    if (event.type === "plan") latestPlan = event;
    else if (latestPlan && TURN_ENDS.includes(event.type)) {
      finalPlans.add(latestPlan.id);
      latestPlan = null;
    }
  }

  const tokens = turnTokens(events);
  let lastUsage: SessionEvent | null = null;
  let lastTokens = 0;
  const rows = events.map((event, index) => {
    const p = event.payload;
    if (p.parent_tool_use_id) return null;
    switch (event.type) {
      case "handoff":
        lastUsage = null;
        return <HandoffDivider key={event.id} sessionId={sessionId} event={event} />;
      case "handoff_offer":
        return (
          <HandoffOffer
            key={event.id}
            agent={p.agent}
            model={p.model}
            // Only the latest offer can be taken, while the session still waits on it.
            active={index === events.length - 1 && status === "waiting_for_input"}
            onAccept={onAcceptHandoff}
          />
        );
      case "user_message":
        return (
          <div key={event.id} id={eventAnchor(event.id)} className="row user-message">
            <span className="prompt-mark">❯</span>
            <span className="pre-wrap">{p.text}</span>
          </div>
        );
      case "message":
        return (
          <div key={event.id} className="row assistant-message">
            <MarkdownText>{p.text}</MarkdownText>
          </div>
        );
      case "plan":
        return finalPlans.has(event.id) ? <PlanRow key={event.id} items={p.items ?? []} /> : null;
      case "tool_use":
        return (
          <ToolRow
            key={event.id}
            name={p.name}
            input={p.input}
            projectPath={projectPath}
            result={results.get(p.tool_use_id)}
            denied={denied.has(p.tool_use_id)}
            pending={running}
          >
            {isSubagent(event) && (
              <>
                <div className="tool-label">Steps</div>
                <SubagentSteps
                  steps={steps.get(p.tool_use_id) ?? []}
                  outcomes={outcomes}
                  projectPath={projectPath}
                  running={running}
                />
              </>
            )}
          </ToolRow>
        );
      case "usage":
        lastUsage = event;
        lastTokens = tokens.get(event.id) ?? 0;
        return null;
      case "question":
        return (
          <div key={event.id} className="row turn-end question-end">
            <span>Waiting for your answer</span>
            <UsageSummary usage={lastUsage} tokens={lastTokens} />
          </div>
        );
      case "completed":
        return (
          <div key={event.id} className="row turn-end">
            <span>✓ Done{p.permission_denials ? ` · ${p.permission_denials} action(s) denied` : ""}</span>
            <UsageSummary usage={lastUsage} tokens={lastTokens} />
          </div>
        );
      case "limit_reached":
        return (
          <div key={event.id} className="row notice warning">
            {p.simulated ? (
              <>
                Simulated usage limit for {agentName(event.agent)} (<code>HUB_SIMULATE_LIMIT</code> is set). Its turn ran
                normally; the hub treats it as out of usage.
              </>
            ) : (
              <>
                Usage limit reached{p.resets_at ? `; resets ${new Date(p.resets_at * 1000).toLocaleString()}` : ""}.
              </>
            )}
          </div>
        );
      case "error":
        return p.stopped ? (
          <div key={event.id} className="row turn-end">
            <span>■ Stopped</span>
          </div>
        ) : (
          <div key={event.id} className="row notice error">
            <strong>Error:</strong> {p.message || p.subtype || "Something went wrong"}
            {p.stderr && <pre className="tool-detail">{p.stderr}</pre>}
          </div>
        );
      default:
        // started, tool_result, permission_denied and rate_limit are shown elsewhere.
        return null;
    }
  });

  return (
    <div className="transcript">
      {header}
      {rows}
      {(status === "working" || status === "handing_off") && (
        <div className="row working">
          <span className="spinner" aria-hidden="true" />{" "}
          {status === "handing_off" ? "Writing the handoff brief…" : "Working…"}
        </div>
      )}
    </div>
  );
}

/** The id of a transcript row that starts a turn, so the history can scroll to it. */
export function eventAnchor(eventId: number): string {
  return `event-${eventId}`;
}

function HandoffDivider({ sessionId, event }: { sessionId: string; event: SessionEvent }) {
  const brief = useBrief(sessionId, event.stint_id);
  const p = event.payload;
  return (
    <div id={eventAnchor(event.id)} className="row handoff">
      <div className="handoff-line">
        <span>
          {agentName(p.from_agent)} → {agentName(p.to_agent)}
          {p.model ? ` (${p.model})` : ""} · {REASONS[p.reason] ?? p.reason}
        </span>
        {brief.button}
      </div>
      {brief.panel}
    </div>
  );
}

/** A "View brief" button and the brief it shows, loaded the first time it is opened. */
export function useBrief(sessionId: string, stintId: number | null): { button: ReactNode; panel: ReactNode } {
  const [brief, setBrief] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    setOpen(!open);
    if (brief === null && stintId !== null) {
      try {
        setBrief((await api.getStint(sessionId, stintId)).brief ?? "");
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
  }

  return {
    button: (
      <button className="button subtle" onClick={() => void toggle()} aria-expanded={open}>
        {open ? "Hide brief" : "View brief"}
      </button>
    ),
    panel: open && (
      <div className="handoff-brief">
        {error ? (
          <span className="muted">{error}</span>
        ) : brief === null ? (
          <span className="muted">Loading…</span>
        ) : (
          <MarkdownText>{brief}</MarkdownText>
        )}
      </div>
    ),
  };
}

interface HandoffOfferProps {
  agent: AgentKind;
  model: string | null;
  active: boolean;
  onAccept: (agent: AgentKind, model: string | null) => Promise<void>;
}

function HandoffOffer({ agent, model, active, onAccept }: HandoffOfferProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = `${agentName(agent)}${model ? ` (${model})` : ""}`;
  return (
    <div className="row notice warning handoff-offer">
      <span>Next in the handoff chain: {name}.</span>
      {active && (
        <button
          className="button primary"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              await onAccept(agent, model);
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
              setBusy(false);
            }
          }}
        >
          Hand off to {name}
        </button>
      )}
      {error && <span className="muted">{error}</span>}
    </div>
  );
}

export function agentName(agent: string): string {
  return AGENT_NAMES[agent as AgentKind] ?? agent;
}

/** The plan as it stood at the end of a turn: "Plan · 3 of 5 done", with the list on click. */
function PlanRow({ items }: { items: PlanItem[] }) {
  const [open, setOpen] = useState(false);
  const { done, total } = planCounts(items);
  return (
    <div className="row tool">
      <button className="tool-summary" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="tool-dot tool-done" aria-hidden="true" />
        <span className="tool-name">Plan</span>
        <span className="tool-arg">
          {done} of {total} done
        </span>
        <span className="tool-toggle" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="tool-body">
          <PlanList items={items} />
        </div>
      )}
    </div>
  );
}

export const PLAN_LABELS: Record<string, string> ={ completed: "Done", in_progress: "In progress", pending: "Pending" };

export function PlanList({ items }: { items: PlanItem[] }) {
  return (
    <ul className="plan-list">
      {items.map((item, i) => (
        <li key={i} className={`plan-item ${item.status}`}>
          <span className="plan-mark" role="img" aria-label={PLAN_LABELS[item.status] ?? item.status} />
          <span>{item.text}</span>
        </li>
      ))}
    </ul>
  );
}

/** A subagent's own messages and tool calls, in order. */
export function SubagentSteps(props: { steps: SessionEvent[]; outcomes: ToolOutcomes; projectPath: string; running: boolean }) {
  const { steps, outcomes, projectPath, running } = props;
  if (steps.length === 0) return <p className="muted small subagent-steps">No steps yet.</p>;
  return (
    <div className="subagent-steps">
      {steps.map((event) =>
        event.type === "message" ? (
          <div key={event.id} className="row assistant-message">
            <MarkdownText>{event.payload.text}</MarkdownText>
          </div>
        ) : (
          <ToolRow
            key={event.id}
            name={event.payload.name}
            input={event.payload.input}
            projectPath={projectPath}
            result={outcomes.results.get(event.payload.tool_use_id)}
            denied={outcomes.denied.has(event.payload.tool_use_id)}
            pending={running}
          />
        ),
      )}
    </div>
  );
}

interface ToolRowProps {
  name: string;
  input: Record<string, any>;
  projectPath: string;
  result?: SessionEvent;
  denied: boolean;
  pending: boolean;
  // Shown under the result when the row is open (a subagent's steps).
  children?: ReactNode;
}

export function ToolRow({ name, input, projectPath, result, denied, pending, children }: ToolRowProps) {
  const [open, setOpen] = useState(false);
  const state = denied ? "denied" : result?.payload.is_error ? "failed" : result ? "done" : pending ? "running" : "done";

  return (
    <div className="row tool">
      <button className="tool-summary" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`tool-dot tool-${state}`} aria-hidden="true" />
        <span className="tool-name">{name}</span>
        <span className="tool-arg">{summarize(input, projectPath)}</span>
        {denied && <span className="tool-flag">denied: needs approval</span>}
        <span className="tool-toggle" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (
        <div className="tool-body">
          <div className="tool-label">Input</div>
          <pre className="tool-detail">{JSON.stringify(input, null, 2)}</pre>
          {result && (
            <>
              <div className="tool-label">Result</div>
              <pre className="tool-detail">{truncate(result.payload.content || "(empty)", 4000)}</pre>
            </>
          )}
          {children}
        </div>
      )}
    </div>
  );
}

/** `tokens` is this turn's own count, shown for every agent so they can be compared. */
function UsageSummary({ usage, tokens }: { usage: SessionEvent | null; tokens: number }) {
  if (!usage) return null;
  const { cost_usd, num_turns, duration_ms } = usage.payload;
  const parts = [
    typeof cost_usd === "number" ? (
      <span title="What this turn would cost at API prices. Subscription use is not billed per turn.">
        ${cost_usd.toFixed(4)}
      </span>
    ) : null,
    tokens ? `${formatTokens(tokens)} tokens` : null,
    num_turns ? `${num_turns} step${num_turns === 1 ? "" : "s"}` : null,
    duration_ms ? `${(duration_ms / 1000).toFixed(1)}s` : null,
  ].filter(Boolean);
  return (
    <span className="muted">
      {parts.map((part, i) => (
        <Fragment key={i}>
          {i > 0 && " · "}
          {part}
        </Fragment>
      ))}
    </span>
  );
}

/** One short line describing a tool call, like Claude Code's `Write(color.txt)`. */
export function summarize(input: Record<string, any>, projectPath: string): string {
  if (Array.isArray(input.changes)) return truncate(fileChanges(input.changes, projectPath), 120);
  const value =
    input.command ?? input.file_path ?? input.path ?? input.pattern ?? input.url ?? input.query ?? input.description;
  const text = typeof value === "string" ? relativeTo(projectPath, value) : JSON.stringify(input);
  return truncate(text.replace(/\s+/g, " "), 120);
}

const CHANGE_KINDS: Record<string, string> = { add: "new", delete: "deleted" };

/** Codex's file_change: each file relative to the project, marked when it was added or deleted. */
function fileChanges(changes: { path?: string; kind?: string }[], projectPath: string): string {
  const files = changes
    .filter((c) => c.path)
    .map((c) => {
      const kind = CHANGE_KINDS[c.kind ?? ""];
      return `${relativeTo(projectPath, c.path!)}${kind ? ` (${kind})` : ""}`;
    });
  return files.join(", ") || "(no files)";
}

/** Shows paths inside the project relative to it; Windows paths compare case-insensitively. */
export function relativeTo(root: string, path: string): string {
  const normalize = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const base = normalize(root);
  const target = normalize(path);
  if (target === base) return ".";
  return target.startsWith(`${base}/`) ? path.slice(base.length + 1) : path;
}

export function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
