import { useState } from "react";
import Markdown from "react-markdown";
import type { SessionEvent, SessionStatus } from "../api";

interface Props {
  events: SessionEvent[];
  status: SessionStatus;
  projectPath: string;
}

/** Renders a session's events as a conversation, pairing each tool call with its result. */
export function Transcript({ events, status, projectPath }: Props) {
  const results = new Map<string, SessionEvent>();
  const denied = new Set<string>();
  for (const event of events) {
    if (event.type === "tool_result") results.set(event.payload.tool_use_id, event);
    if (event.type === "permission_denied") denied.add(event.payload.tool_use_id);
  }

  let lastUsage: SessionEvent | null = null;
  const rows = events.map((event) => {
    const p = event.payload;
    switch (event.type) {
      case "user_message":
        return (
          <div key={event.id} className="row user-message">
            <span className="prompt-mark">❯</span>
            <span className="pre-wrap">{p.text}</span>
          </div>
        );
      case "message":
        return (
          <div key={event.id} className={`row assistant-message${p.parent_tool_use_id ? " nested" : ""}`}>
            <Markdown>{p.text}</Markdown>
          </div>
        );
      case "tool_use":
        return (
          <ToolRow
            key={event.id}
            name={p.name}
            input={p.input}
            projectPath={projectPath}
            result={results.get(p.tool_use_id)}
            denied={denied.has(p.tool_use_id)}
            pending={status === "working"}
            nested={Boolean(p.parent_tool_use_id)}
          />
        );
      case "usage":
        lastUsage = event;
        return null;
      case "question":
        return (
          <div key={event.id} className="row turn-end question-end">
            <span>Waiting for your answer</span>
            <UsageSummary usage={lastUsage} />
          </div>
        );
      case "completed":
        return (
          <div key={event.id} className="row turn-end">
            <span>✓ Done{p.permission_denials ? ` · ${p.permission_denials} action(s) denied` : ""}</span>
            <UsageSummary usage={lastUsage} />
          </div>
        );
      case "limit_reached":
        return (
          <div key={event.id} className="row notice warning">
            Usage limit reached{p.resets_at ? `; resets ${new Date(p.resets_at * 1000).toLocaleString()}` : ""}.
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
      {rows}
      {status === "working" && (
        <div className="row working">
          <span className="spinner" aria-hidden="true" /> Working…
        </div>
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
  nested: boolean;
}

function ToolRow({ name, input, projectPath, result, denied, pending, nested }: ToolRowProps) {
  const [open, setOpen] = useState(false);
  const state = denied ? "denied" : result?.payload.is_error ? "failed" : result ? "done" : pending ? "running" : "done";

  return (
    <div className={`row tool${nested ? " nested" : ""}`}>
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
        </div>
      )}
    </div>
  );
}

function UsageSummary({ usage }: { usage: SessionEvent | null }) {
  if (!usage) return null;
  const { cost_usd, num_turns, duration_ms, usage: tokens } = usage.payload;
  // Codex reports no price, only tokens (input already includes cached input).
  const tokenCount = (tokens?.input_tokens ?? 0) + (tokens?.output_tokens ?? 0);
  const parts = [
    typeof cost_usd === "number"
      ? `$${cost_usd.toFixed(4)}`
      : tokenCount
        ? `${formatTokens(tokenCount)} tokens`
        : null,
    num_turns ? `${num_turns} step${num_turns === 1 ? "" : "s"}` : null,
    duration_ms ? `${(duration_ms / 1000).toFixed(1)}s` : null,
  ].filter(Boolean);
  return <span className="muted">{parts.join(" · ")}</span>;
}

function formatTokens(count: number): string {
  return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
}

/** One short line describing a tool call, like Claude Code's `Write(color.txt)`. */
function summarize(input: Record<string, any>, projectPath: string): string {
  const value =
    input.command ?? input.file_path ?? input.path ?? input.pattern ?? input.url ?? input.query ?? input.description;
  const text = typeof value === "string" ? relativeTo(projectPath, value) : JSON.stringify(input);
  return truncate(text.replace(/\s+/g, " "), 120);
}

/** Shows paths inside the project relative to it; Windows paths compare case-insensitively. */
function relativeTo(root: string, path: string): string {
  const normalize = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const base = normalize(root);
  const target = normalize(path);
  if (target === base) return ".";
  return target.startsWith(`${base}/`) ? path.slice(base.length + 1) : path;
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text;
}
