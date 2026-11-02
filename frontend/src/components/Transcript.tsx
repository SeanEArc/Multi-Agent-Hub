import { Fragment, useState, type ReactNode } from "react";
import { DiffStat, DiffView, startsOpen } from "./DiffView";
import { MarkdownText } from "./MarkdownText";
import { AGENT_NAMES, api, type AgentKind, type SessionEvent, type SessionStatus } from "../api";
import { COMMAND_TOOLS, commandOutcome, editDiffs, isEditTool } from "../diffs";
import { formatTokens, turnTokens } from "../history";
import {
  editedPaths,
  isSubagent,
  planCounts,
  stepsByParent,
  toolOutcomes,
  type PlanItem,
  type ToolOutcomes,
} from "../progress";

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
  // Files the current turn edited, for its turn-end row.
  let turnFiles = new Set<string>();
  const rows = events.map((event, index) => {
    const p = event.payload;
    if (event.type === "tool_use") for (const path of editedPaths(p.name, p.input ?? {})) turnFiles.add(path);
    if (p.parent_tool_use_id) return null;
    const files = turnFiles.size;
    if (TURN_ENDS.includes(event.type)) turnFiles = new Set();
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
          <TurnEnd key={event.id} outcome="question" label="Waiting for your answer" files={files}>
            <UsageSummary usage={lastUsage} tokens={lastTokens} />
          </TurnEnd>
        );
      case "completed": {
        const denials = p.permission_denials;
        return (
          <TurnEnd
            key={event.id}
            outcome="done"
            label={`Done${denials ? ` · ${denials} action${denials === 1 ? "" : "s"} denied` : ""}`}
            files={files}
          >
            <UsageSummary usage={lastUsage} tokens={lastTokens} />
          </TurnEnd>
        );
      }
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
          <TurnEnd key={event.id} outcome="stopped" label="Stopped" files={files} />
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
  const failed = !!result?.payload.is_error;
  const state = denied ? "denied" : failed ? "failed" : result ? "done" : pending ? "running" : "done";
  const edit = isEditTool(name, input ?? {});
  const diffs = edit ? editDiffs(name, input ?? {}, result) : [];
  // An edit that was refused or failed changed nothing, so its diff is only shown on request.
  const showDiff = edit && !denied && !failed && diffs.some((d) => d.diff !== null);
  // A one-file edit opens with its row, small ones by default; a patch to several files is open,
  // with a dropdown per file.
  const multiFile = showDiff && diffs.length > 1;
  // null until the user clicks: the default follows the diff, which can arrive after the row.
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open = toggled ?? (showDiff && (multiFile || startsOpen(diffs)));
  const setOpen = (value: boolean) => setToggled(value);
  const outcome = COMMAND_TOOLS.includes(name) && result ? commandOutcome(result) : null;
  const resultText = result && <ResultText content={result.payload.content} />;

  return (
    <div className="row tool">
      <button className="tool-summary" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className={`tool-dot tool-${state}`} aria-hidden="true" />
        <span className="tool-name">{name}</span>
        <span className="tool-arg">{summarize(input, projectPath)}</span>
        {showDiff && <DiffStat diffs={diffs} />}
        {outcome && outcome.exitCode !== 0 && !denied && (
          <span className="tool-exit">{outcome.exitCode === null ? "failed" : `exit ${outcome.exitCode}`}</span>
        )}
        {denied && <span className="tool-flag">denied: needs approval</span>}
        <span className="tool-toggle" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {showDiff && open && (
        <div className="tool-preview">
          <DiffView diffs={diffs} projectPath={projectPath} collapsible={multiFile} showPaths={multiFile} />
        </div>
      )}
      {outcome && !open && !denied && outcome.output && (
        <OutputPreview output={outcome.output} failed={outcome.exitCode !== 0} />
      )}
      {/* A successful edit's diff above is all there is to show, unless it started a subagent. */}
      {open && (!showDiff || children) && (
        <div className="tool-body">
          {outcome && !denied ? (
            <>
              <div className="tool-label">Command</div>
              <pre className="tool-detail">{input.command}</pre>
              <div className="tool-label">Output{outcome.exitCode !== null ? ` · exit code ${outcome.exitCode}` : ""}</div>
              <pre className="tool-detail">{truncate(outcome.output || "(no output)", 20000)}</pre>
            </>
          ) : edit ? (
            <>
              {!showDiff && <DiffView diffs={diffs} projectPath={projectPath} />}
              {/* The diff says what a successful edit did; the result text only matters when it didn't. */}
              {!showDiff && resultText}
            </>
          ) : (
            <>
              <div className="tool-label">Input</div>
              <pre className="tool-detail">{JSON.stringify(input, null, 2)}</pre>
              {resultText}
            </>
          )}
          {children}
        </div>
      )}
    </div>
  );
}

function ResultText({ content }: { content: string }) {
  return (
    <>
      <div className="tool-label">Result</div>
      <pre className="tool-detail">{truncate(content || "(empty)", 4000)}</pre>
    </>
  );
}

const PREVIEW_LINES = 4;

/**
 * A few lines of a command's output under its row: the start, and for a failed command also
 * the end, since an error can be at either.
 */
function OutputPreview({ output, failed }: { output: string; failed: boolean }) {
  const lines = output.split("\n").map((line) => truncate(line, 300));
  const head = lines.length <= PREVIEW_LINES ? lines : lines.slice(0, failed ? PREVIEW_LINES / 2 : PREVIEW_LINES - 1);
  const tail = failed && lines.length > PREVIEW_LINES ? lines.slice(-PREVIEW_LINES / 2) : [];
  const more = lines.length - head.length - tail.length;
  return (
    <div className={`tool-output-preview${failed ? " failed" : ""}`}>
      <pre>{head.join("\n")}</pre>
      {more > 0 && <div className="tool-output-more">… {more} more line{more === 1 ? "" : "s"}</div>}
      {tail.length > 0 && <pre>{tail.join("\n")}</pre>}
    </div>
  );
}

type TurnOutcome = "done" | "question" | "stopped";
const TURN_END_MARKS: Record<TurnOutcome, string> = { done: "✓", question: "?", stopped: "■" };

/** The line that closes a turn: how it ended and how many files it edited, then its usage. */
function TurnEnd(props: { outcome: TurnOutcome; label: string; files: number; children?: ReactNode }) {
  const { outcome, label, files, children } = props;
  return (
    <div className={`row turn-end ${outcome}`}>
      <span className="turn-end-label">
        <span className="turn-end-mark" aria-hidden="true">
          {TURN_END_MARKS[outcome]}
        </span>
        <span>{label}</span>
        {files > 0 && (
          <span className="muted">
            · {files} file{files === 1 ? "" : "s"} edited
          </span>
        )}
      </span>
      {children}
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
