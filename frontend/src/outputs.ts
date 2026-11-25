// Where in the conversation agents' output files were created or edited.
import type { OutputFile, SessionEvent } from "./api";
import { parseTime } from "./format";

/** One file's change, as the backend saves it on the tool result (or turn end) that followed it (`output_files`). */
export interface OutputChange {
  name: string;
  change: "created" | "edited";
}

/**
 * The output files each event shows, by event id. Files no event names (written before the hub recorded this) are
 * placed by their time: at the first tool result or turn end after they were last written.
 */
export function outputChanges(events: SessionEvent[], files: OutputFile[], running: boolean): Map<number, OutputChange[]> {
  const changes = new Map<number, OutputChange[]>();
  const named = new Set<string>();
  for (const event of events) {
    const saved = event.payload.output_files;
    if (!Array.isArray(saved) || !saved.length) continue;
    changes.set(event.id, saved);
    for (const change of saved) named.add(change.name);
  }
  for (const file of files) {
    if (named.has(file.name)) continue;
    const after = events.find(
      (e) =>
        ["tool_result", "completed", "question", "error", "limit_reached"].includes(e.type) &&
        parseTime(e.created_at).getTime() >= file.modified * 1000,
    );
    // While a turn runs, the event that names the file may still be on its way.
    const place = after ?? (running ? undefined : events.at(-1));
    if (place) changes.set(place.id, [...(changes.get(place.id) ?? []), { name: file.name, change: "created" }]);
  }
  return changes;
}
