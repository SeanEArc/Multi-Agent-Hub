import { useState } from "react";
import { DiffStat, DiffView } from "./DiffView";
import { formatClock, formatTime } from "../format";
import { planCounts, type FileEdit, type FileTouched, type PlanItem, type Progress, type Subagent, type ToolOutcomes } from "../progress";
import { PlanList, SubagentSteps, agentName, relativeTo } from "./Transcript";

interface Props {
  progress: Progress;
  outcomes: ToolOutcomes;
  projectPath: string;
  running: boolean;
}

/** Layout A: the plan and subagents in a card above the transcript. Hidden until the agent has either. */
export function ProgressCard({ progress, outcomes, projectPath, running }: Props) {
  const [collapsed, setCollapsed] = useState(false);
  const { plan, subagents } = progress;
  if (!plan?.length && subagents.length === 0) return null;

  return (
    <section className="progress-card" aria-label="Progress">
      <div className="progress-card-header">
        {plan?.length ? <PlanHeading plan={plan} /> : <strong>Subagents</strong>}
        <button className="button subtle" onClick={() => setCollapsed(!collapsed)} aria-expanded={!collapsed}>
          {collapsed ? "Show" : "Hide"}
        </button>
      </div>
      {!collapsed && (
        <>
          {plan?.length ? <PlanList items={plan} /> : null}
          {subagents.length > 0 && (
            <div className={plan?.length ? "progress-subagents divided" : "progress-subagents"}>
              {plan?.length ? <span className="muted small">Subagents</span> : null}
              {subagents.map((s) => (
                <SubagentRow key={s.event.id} subagent={s} outcomes={outcomes} projectPath={projectPath} running={running} />
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}

/** Layout C: a side panel with the plan, subagents and every file the session changed. */
export function ProgressAside({ progress, outcomes, projectPath, running }: Props) {
  const { plan, subagents, files } = progress;
  return (
    <aside className="progress-aside" aria-label="Progress">
      <section>
        {plan?.length ? (
          <>
            <PlanHeading plan={plan} />
            <PlanList items={plan} />
          </>
        ) : (
          <>
            <h2>Plan</h2>
            <p className="muted small">The agent hasn't made a to-do list for this task.</p>
          </>
        )}
      </section>
      <section>
        <h2>Subagents</h2>
        {subagents.length ? (
          <div className="progress-subagents">
            {subagents.map((s) => (
              <SubagentRow key={s.event.id} subagent={s} outcomes={outcomes} projectPath={projectPath} running={running} />
            ))}
          </div>
        ) : (
          <p className="muted small">None in this turn.</p>
        )}
      </section>
      <section>
        <h2>Files changed</h2>
        {files.length ? (
          <ul className="progress-files">
            {files.map((file) => (
              <FileHistory key={file.path} file={file} projectPath={projectPath} />
            ))}
          </ul>
        ) : (
          <p className="muted small">No edits yet.</p>
        )}
      </section>
    </aside>
  );
}

/** A file the session changed; opens into each edit with its time, agent and diff. */
function FileHistory({ file, projectPath }: { file: FileTouched; projectPath: string }) {
  const [open, setOpen] = useState(false);
  const diffs = file.edits.flatMap((e) => (e.diff ? [e.diff] : []));
  const count = file.edits.length;
  return (
    <li className="file-history">
      <button className="file-history-summary" onClick={() => setOpen(!open)} aria-expanded={open} title={file.path}>
        <span className="tool-toggle" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        <span className="progress-file-path">{relativeTo(projectPath, file.path)}</span>
        <DiffStat diffs={diffs} />
        <span className="muted">
          {count} edit{count === 1 ? "" : "s"}
        </span>
      </button>
      {open && (
        <ol className="file-edits">
          {file.edits.map((edit, i) => (
            <li key={`${edit.event.id}-${i}`}>
              {edit.diff ? (
                // Each edit is a dropdown, open when it's small.
                <DiffView
                  diffs={[edit.diff]}
                  projectPath={projectPath}
                  collapsible
                  label={<EditLabel edit={edit} />}
                />
              ) : (
                <div className="file-edit-meta muted">
                  <EditLabel edit={edit} />
                  <span>no diff recorded</span>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </li>
  );
}

function EditLabel({ edit }: { edit: FileEdit }) {
  return (
    <span className="file-edit-label">
      <time dateTime={edit.event.created_at} title={formatTime(edit.event.created_at)}>
        {formatClock(edit.event.created_at)}
      </time>
      <span>{agentName(edit.event.agent)}</span>
    </span>
  );
}

function PlanHeading({ plan }: { plan: PlanItem[] }) {
  const { done, total } = planCounts(plan);
  return (
    <div className="plan-heading">
      <strong>Plan</strong>
      <span className="muted small">
        {done} of {total} done
      </span>
      <span
        className="plan-bar"
        role="progressbar"
        aria-label="Plan progress"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
      >
        <span style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
      </span>
    </div>
  );
}

interface SubagentRowProps {
  subagent: Subagent;
  outcomes: ToolOutcomes;
  projectPath: string;
  running: boolean;
}

function SubagentRow({ subagent, outcomes, projectPath, running }: SubagentRowProps) {
  const [open, setOpen] = useState(false);
  const toolSteps = subagent.steps.filter((e) => e.type === "tool_use").length;
  return (
    <div className="subagent">
      <button className="subagent-summary" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="tool-toggle" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        <strong>{subagent.kind}</strong>
        <span className="subagent-description">{subagent.description}</span>
        <span className={`subagent-state ${subagent.state}`}>
          {toolSteps} step{toolSteps === 1 ? "" : "s"} · {subagent.state}
        </span>
      </button>
      {open && <SubagentSteps steps={subagent.steps} outcomes={outcomes} projectPath={projectPath} running={running} />}
    </div>
  );
}
