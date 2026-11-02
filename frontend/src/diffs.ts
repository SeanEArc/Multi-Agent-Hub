// Edits as diffs and commands with their outcome, read from a tool call and its result.
import type { SessionEvent } from "./api";

/** One file's change, as the backend saves it on an edit's tool result (`diffs`). */
export interface FileDiff {
  path: string;
  kind: "add" | "update" | "delete";
  // Unified diff hunks; null when the hub couldn't tell what changed.
  diff: string | null;
  // "commit": the first change the hub saw to this file, compared with the last commit.
  base?: "commit" | "previous_edit" | null;
  truncated?: boolean;
}

export interface DiffLine {
  kind: "add" | "del" | "context" | "hunk";
  text: string;
  oldNo?: number;
  newNo?: number;
}

export const COMMAND_TOOLS = ["Bash", "PowerShell", "Shell"];
const EDIT_TOOLS = ["Edit", "Write", "MultiEdit"];

export function isEditTool(name: string, input: Record<string, any>): boolean {
  return EDIT_TOOLS.includes(name) || Array.isArray(input?.changes);
}

/**
 * An edit's diffs: the ones the backend saved, or for events saved before it did, rebuilt from
 * Claude's input (old and new text, with no line numbers). Codex's older patches have no diff.
 */
export function editDiffs(name: string, input: Record<string, any>, result?: SessionEvent): FileDiff[] {
  const saved = result?.payload.diffs;
  if (Array.isArray(saved)) return saved;
  if (Array.isArray(input?.changes)) {
    return input.changes
      .filter((c: any) => c.path)
      .map((c: any) => ({ path: c.path, kind: c.kind === "add" || c.kind === "delete" ? c.kind : "update", diff: null }));
  }
  const path = input?.file_path;
  if (typeof path !== "string") return [];
  if (name === "Write" && typeof input.content === "string") {
    return [{ path, kind: "update", diff: textDiff("", input.content) }];
  }
  if (name === "Edit" && typeof input.old_string === "string") {
    return [{ path, kind: "update", diff: textDiff(input.old_string, input.new_string ?? "") }];
  }
  if (name === "MultiEdit" && Array.isArray(input.edits)) {
    const parts = input.edits.map((e: any) => textDiff(e.old_string ?? "", e.new_string ?? ""));
    return [{ path, kind: "update", diff: parts.join("\n") }];
  }
  return [];
}

/** Old text removed and new text added, without line numbers or context: a stand-in when no real diff was saved. */
function textDiff(oldText: string, newText: string): string {
  const lines = (text: string, mark: string) => (text ? text.replace(/\n$/, "").split("\n").map((l) => mark + l) : []);
  return ["@@", ...lines(oldText, "-"), ...lines(newText, "+")].join("\n");
}

/** Diff text as lines with old and new line numbers (none after a bare "@@"). */
export function parseDiff(diff: string): DiffLine[] {
  const out: DiffLine[] = [];
  let oldNo: number | undefined;
  let newNo: number | undefined;
  for (const text of diff.split("\n")) {
    if (text.startsWith("@@")) {
      const m = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
      // A file that starts empty reports line 0; its first line is line 1.
      oldNo = m ? Math.max(1, Number(m[1])) : undefined;
      newNo = m ? Math.max(1, Number(m[2])) : undefined;
      // Hunk headers are shown as gaps between hunks, so the first one is only needed for its numbers.
      if (out.length) out.push({ kind: "hunk", text: "" });
    } else if (text.startsWith("+")) {
      out.push({ kind: "add", text: text.slice(1), newNo });
      if (newNo !== undefined) newNo++;
    } else if (text.startsWith("-")) {
      out.push({ kind: "del", text: text.slice(1), oldNo });
      if (oldNo !== undefined) oldNo++;
    } else if (!text.startsWith("\\")) {
      // "\ No newline at end of file" is left out.
      out.push({ kind: "context", text: text.slice(1), oldNo, newNo });
      if (oldNo !== undefined) oldNo++;
      if (newNo !== undefined) newNo++;
    }
  }
  return out;
}

export function diffCounts(diffs: FileDiff[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const d of diffs) {
    for (const line of d.diff?.split("\n") ?? []) {
      if (line.startsWith("+")) added++;
      else if (line.startsWith("-")) removed++;
    }
  }
  return { added, removed };
}

export interface CommandOutcome {
  // null while running, or when the agent didn't report it.
  exitCode: number | null;
  output: string;
}

// Claude starts a failed command's result with its exit code; a command with no output gets a placeholder.
const CLAUDE_EXIT = /^(?:Error: )?Exit code (\d+)\n?/;
const NO_OUTPUT = /^\((?:Bash|PowerShell) completed with no output\)$/;

export function commandOutcome(result: SessionEvent): CommandOutcome {
  const p = result.payload;
  let output: string = p.content ?? "";
  let exitCode: number | null = typeof p.exit_code === "number" ? p.exit_code : null;
  const match = CLAUDE_EXIT.exec(output);
  if (match) {
    exitCode ??= Number(match[1]);
    output = output.slice(match[0].length);
  }
  if (NO_OUTPUT.test(output.trim())) output = "";
  if (exitCode === null && !p.is_error) exitCode = 0;
  return { exitCode, output: output.replace(/\r\n/g, "\n").replace(/\s+$/, "") };
}
