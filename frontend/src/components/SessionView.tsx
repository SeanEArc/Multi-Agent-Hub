import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AGENT_NAMES, ApiError, api, workdir, type AgentSession, type ProgressLayout, type SessionEvent } from "../api";
import { deriveProgress, toolOutcomes } from "../progress";
import { useSessionStream } from "../useSessionStream";
import { Composer } from "./Composer";
import { HandoffDialog } from "./HandoffDialog";
import { HistoryView } from "./HistoryView";
import { ProgressAside, ProgressCard } from "./ProgressPanel";
import { StatusBadge } from "./StatusBadge";
import { Transcript, eventAnchor } from "./Transcript";
import { UsageMeters } from "./UsageMeters";
import { WorktreeDialog } from "./WorktreeDialog";

interface Props {
  session: AgentSession;
  onChanged: () => void;
  onDeleted: () => void;
  layout: ProgressLayout;
  onLayoutChange: (layout: ProgressLayout) => void;
}

export function SessionView({ session, onChanged, onDeleted, layout, onLayoutChange }: Props) {
  const { events, status: liveStatus, connected } = useSessionStream(session.id);
  const status = liveStatus ?? session.status;
  const scroller = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const [handingOff, setHandingOff] = useState(false);
  const [showWorktree, setShowWorktree] = useState(false);
  const [view, setView] = useState<"transcript" | "history">("transcript");
  // A turn picked in the history, to scroll to once the transcript is back.
  const [jumpTo, setJumpTo] = useState<number | null>(null);

  // Keep the sidebar (and this session's current agent) in step when the live status changes.
  useEffect(() => {
    if (liveStatus && liveStatus !== session.status) onChanged();
  }, [liveStatus]);

  // A handoff starts a new stint; refresh so the header shows the new agent.
  const currentStintId = session.stints.at(-1)?.id;
  const latestStintId = events.at(-1)?.stint_id;
  useEffect(() => {
    if (latestStintId && currentStintId && latestStintId > currentStintId) onChanged();
  }, [latestStintId, currentStintId]);

  // Follow new events unless the user scrolled up to read, or show the turn picked in the history.
  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    if (jumpTo !== null) {
      const row = document.getElementById(eventAnchor(jumpTo));
      setJumpTo(null);
      if (row) {
        stickToBottom.current = false;
        // Centered, so the sticky progress card above the transcript doesn't cover it.
        row.scrollIntoView({ block: "center" });
        row.classList.add("jump-highlight");
        setTimeout(() => row.classList.remove("jump-highlight"), 2000);
        return;
      }
    }
    if (stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [events.length, status, view, jumpTo]);

  function showView(next: "transcript" | "history") {
    if (next === "transcript") stickToBottom.current = true;
    setView(next);
  }

  // The model belongs to the agent working now, not one before a handoff.
  const currentEvents = events.filter((e) => e.stint_id === currentStintId);
  const reported = lastOf(currentEvents, "started", "model");
  const reportedModel: string | undefined = reported?.payload.model ?? undefined;
  const lastUsageReport = lastOf(events, "rate_limit", "limit_reached");
  const agentName = AGENT_NAMES[session.agent];
  const busy = status === "working" || status === "handing_off";
  const folder = workdir(session);
  const worktree = session.worktree;
  const outcomes = toolOutcomes(events);
  const progress = deriveProgress(events, outcomes, currentStintId, status === "working");
  const progressProps = { progress, outcomes, projectPath: folder, running: status === "working" };

  // The agent names its model once a turn starts (Codex after it ends); show it in the sidebar too.
  useEffect(() => {
    if (reportedModel && reportedModel !== session.reported_model) onChanged();
  }, [reportedModel, session.reported_model]);

  async function remove() {
    const files = worktree?.status === "active"
      ? `Its worktree folder is removed too; the branch ${worktree.branch} is kept.`
      : "Files in the project folder are not touched.";
    if (!confirm(`Delete "${session.title}" and its history? ${files}`)) return;
    try {
      await api.deleteSession(session.id);
    } catch (err) {
      if (err instanceof ApiError && err.detail?.changes) {
        alert("The worktree has uncommitted changes. Open Worktree to commit or discard them, then delete the session.");
        return;
      }
      throw err;
    }
    onDeleted();
  }

  return (
    <section className="session-view">
      <header className="session-header">
        <div className="session-heading">
          <h1>{session.title}</h1>
          <div className="session-meta">
            <span className="chip">{agentName}</span>
            <span className="chip">{reportedModel ?? session.reported_model ?? session.model ?? "default model"}</span>
            {worktree && (
              <button
                className={`chip chip-button${worktree.status === "removed" ? " removed" : ""}`}
                onClick={() => setShowWorktree(true)}
                title={worktree.status === "active" ? "Worktree branch" : "The worktree was removed; the branch is kept"}
              >
                ⑂ {worktree.branch}
              </button>
            )}
            <span className="path" title={folder}>
              {session.project_path}
            </span>
          </div>
        </div>
        <div className="session-actions">
          <UsageMeters refreshKey={lastUsageReport?.id} current={session.agent} />
          {!connected && <span className="muted small">Reconnecting…</span>}
          <StatusBadge status={status} />
          <div className="layout-switch" role="group" aria-label="View">
            {(["transcript", "history"] as const).map((option) => (
              <button
                key={option}
                aria-pressed={view === option}
                onClick={() => showView(option)}
                title={option === "transcript" ? "The conversation" : "A summary, then each agent's turns and handoffs"}
              >
                {option === "transcript" ? "Transcript" : "History"}
              </button>
            ))}
          </div>
          <div className="layout-switch" role="group" aria-label="Progress layout">
            {(["panel", "split"] as const).map((option) => (
              <button
                key={option}
                aria-pressed={layout === option}
                onClick={() => onLayoutChange(option)}
                title={option === "panel" ? "Plan and subagents above the transcript" : "Plan, subagents and files in a side panel"}
              >
                {option === "panel" ? "Panel" : "Split"}
              </button>
            ))}
          </div>
          <button
            className="button"
            onClick={() => setHandingOff(true)}
            disabled={busy}
            title={busy ? "Stop the running turn to hand off" : "Move this session to another agent"}
          >
            Hand off
          </button>
          <button className="button subtle" onClick={() => void remove()} title="Delete session">
            Delete
          </button>
        </div>
      </header>

      <div className={`session-body ${layout}`}>
        <div className="session-main">
          {view === "history" ? (
            // Its own element, so it opens at the top rather than at the transcript's scroll position.
            <div key="history" className="transcript-scroller">
              <HistoryView
                session={session}
                events={events}
                status={status}
                projectPath={folder}
                onShowInTranscript={(eventId) => {
                  setView("transcript");
                  setJumpTo(eventId);
                }}
                onChanged={onChanged}
              />
            </div>
          ) : (
            <div
              className="transcript-scroller"
              ref={scroller}
              onScroll={(e) => {
                const el = e.currentTarget;
                stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              }}
            >
              {events.length === 0 && status === "ready" ? (
                <div className="empty-state">
                  <p>Send a message to start {agentName} in</p>
                  <code>{folder}</code>
                </div>
              ) : (
                <Transcript
                  sessionId={session.id}
                  events={events}
                  status={status}
                  projectPath={folder}
                  onAcceptHandoff={async (agent, model) => {
                    stickToBottom.current = true;
                    await api.handoff(session.id, { agent, model });
                    onChanged();
                  }}
                  // In the split layout the card stands in for the side panel on narrow windows.
                  header={<ProgressCard {...progressProps} />}
                />
              )}
            </div>
          )}

          <Composer
            status={status}
            agentName={agentName}
            onSend={async (text) => {
              showView("transcript");
              await api.sendMessage(session.id, text);
              onChanged();
            }}
            onStop={async () => {
              await api.stop(session.id);
              onChanged();
            }}
          />
        </div>
        {layout === "split" && <ProgressAside {...progressProps} />}
      </div>

      {showWorktree && worktree && (
        <WorktreeDialog
          session={session}
          worktree={worktree}
          busy={busy}
          onDone={onChanged}
          onClose={() => setShowWorktree(false)}
        />
      )}

      {handingOff && (
        <HandoffDialog
          session={session}
          onDone={() => {
            stickToBottom.current = true;
            onChanged();
          }}
          onClose={() => setHandingOff(false)}
        />
      )}
    </section>
  );
}

function lastOf(events: SessionEvent[], ...types: string[]): SessionEvent | undefined {
  for (let i = events.length - 1; i >= 0; i--) if (types.includes(events[i].type)) return events[i];
  return undefined;
}
