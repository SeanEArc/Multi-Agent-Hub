import { useState, type ReactNode } from "react";
import { diffCounts, parseDiff, type FileDiff } from "../diffs";
import { relativeTo } from "./Transcript";

const KIND_LABELS: Record<string, string> = { add: "new", delete: "deleted" };
// A diff starts open when it adds and removes at most this many lines, and closed when it changes more.
export const OPEN_DIFF_LINES = 20;

/** Whether a diff starts open: small changes are shown, large ones wait for a click. */
export function startsOpen(diffs: FileDiff[]): boolean {
  const { added, removed } = diffCounts(diffs);
  return added + removed <= OPEN_DIFF_LINES;
}

/** "+12 −3", for a row's summary line. */
export function DiffStat({ diffs }: { diffs: FileDiff[] }) {
  const { added, removed } = diffCounts(diffs);
  if (!added && !removed) return null;
  return (
    <span className="diff-stat">
      {added > 0 && <span className="diff-stat-add">+{added}</span>}
      {removed > 0 && <span className="diff-stat-del">−{removed}</span>}
    </span>
  );
}

interface Props {
  diffs: FileDiff[];
  projectPath: string;
  // Each file's header opens and closes its diff; otherwise the diffs are always shown.
  collapsible?: boolean;
  // Shown in the header instead of the file name (the side panel's time and agent).
  label?: ReactNode;
  // A file name in the header; off when the row around it already names the one file.
  showPaths?: boolean;
}

/** One or more files' diffs, with line numbers and added and removed lines marked. */
export function DiffView({ diffs, projectPath, collapsible = false, label, showPaths = true }: Props) {
  return (
    <div className="diff-list">
      {diffs.map((d, i) => (
        <FileDiffView
          key={`${d.path}-${i}`}
          diff={d}
          projectPath={projectPath}
          collapsible={collapsible}
          label={label}
          showPath={showPaths}
        />
      ))}
    </div>
  );
}

interface FileProps {
  diff: FileDiff;
  projectPath: string;
  collapsible: boolean;
  label?: ReactNode;
  showPath: boolean;
}

function FileDiffView({ diff, projectPath, collapsible, label, showPath }: FileProps) {
  // null until the user clicks: the default follows the diff's size, which can arrive after the row.
  const [toggled, setToggled] = useState<boolean | null>(null);
  const open = !collapsible || (toggled ?? startsOpen([diff]));
  const lines = diff.diff ? parseDiff(diff.diff) : [];
  const kind = KIND_LABELS[diff.kind];
  const name = relativeTo(projectPath, diff.path);
  const title = (
    <>
      {label ?? (showPath && <span className="diff-path">{name}</span>)}
      {kind && <span className={`diff-kind ${diff.kind}`}>{kind}</span>}
      {collapsible && <DiffStat diffs={[diff]} />}
    </>
  );

  return (
    // A new file has no old line numbers, so that column is hidden.
    <div className={`diff${diff.kind === "add" ? " new-file" : ""}${open ? "" : " closed"}`}>
      {collapsible ? (
        <button className="diff-header diff-toggle" onClick={() => setToggled(!open)} aria-expanded={open}>
          <span className="tool-toggle" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
          {title}
        </button>
      ) : (
        (label || showPath || kind) && <div className="diff-header">{title}</div>
      )}
      {open &&
        (diff.diff === null ? (
          <div className="diff-empty muted">
            {diff.kind === "delete"
              ? "File deleted."
              : "No diff available (a binary or large file, or no earlier copy to compare with)."}
          </div>
        ) : (
          <div className="diff-body" role="table" aria-label={`Changes to ${name}`}>
            {lines.map((line, i) =>
              line.kind === "hunk" ? (
                <div key={i} className="diff-gap" role="row" aria-label="Unchanged lines skipped">
                  ⋯
                </div>
              ) : (
                <div key={i} className={`diff-line ${line.kind}`} role="row">
                  <span className="diff-no">{line.oldNo ?? ""}</span>
                  <span className="diff-no">{line.newNo ?? ""}</span>
                  <span className="diff-mark" aria-hidden="true">
                    {line.kind === "add" ? "+" : line.kind === "del" ? "−" : " "}
                  </span>
                  <span className="diff-text">{line.text || " "}</span>
                </div>
              ),
            )}
          </div>
        ))}
      {open && diff.truncated && <div className="diff-empty muted">The rest of this diff was too long to save.</div>}
    </div>
  );
}
