// The preview pane (revision 4, attachments PR 3): what it shows, which engine draws it, and how it opens links.
import { createContext, useContext } from "react";
import { api, type OutputFile } from "./api";
import { fileUrl, kindOf, type Attachment } from "./attachments";
import { inDesktopApp } from "./connection";

/** A file the pane can show: an attachment or an agent's output file. */
export interface PreviewTarget {
  name: string;
  // API path of the bytes, fetched as a Blob with the token.
  url: string;
  // The hub's plain-text copy (attachments of PDF, Word, Excel and CSV), shown when the engine can't open the file.
  textUrl: string | null;
  // Bytes, known before fetching: a large file isn't downloaded whole.
  size: number;
  // Changes when the file does (an agent overwrote an output), so the pane fetches it again and remounts.
  version: string;
  // Set for an output file, so the outputs list can tell the pane it changed.
  outputName?: string;
  // Opens the file with its own app on this PC (the existing open routes).
  open: () => Promise<unknown>;
}

export function attachmentPreview(sessionId: string, attachment: Attachment): PreviewTarget {
  return {
    name: attachment.name,
    url: fileUrl(sessionId, attachment.id),
    textUrl: attachment.text_path ? fileUrl(sessionId, attachment.id, true) : null,
    size: attachment.size,
    version: `attachment:${attachment.id}`,
    open: () => api.openAttachment(sessionId, attachment.id),
  };
}

export function outputPreview(sessionId: string, file: OutputFile): PreviewTarget {
  return {
    name: file.name.split("/").at(-1) ?? file.name,
    url: `/api/sessions/${sessionId}/outputs/file?${new URLSearchParams({ name: file.name })}`,
    textUrl: null,
    size: file.size,
    version: `output:${file.name}:${file.size}:${file.modified}`,
    outputName: file.name,
    open: () => api.openOutput(sessionId, file.name),
  };
}

export type PreviewEngine = "pdf" | "word" | "excel" | "image" | "text";

/** Which engine draws the file, or null for the fallback (text copy and Open): `.xls`, and any other type. */
export function engineFor(name: string): PreviewEngine | null {
  const kind = kindOf(name);
  const lower = name.toLowerCase();
  if (kind === "word") return lower.endsWith(".docx") ? "word" : null;
  if (kind === "excel") return lower.endsWith(".xlsx") ? "excel" : null; // BetterOffice reads xlsx only
  if (kind === "csv") return "text";
  return kind;
}

/** Whether an xlsx holds charts (an `xl/charts/` part). BetterOffice draws charts as blank boxes, so the pane says
 * so. Only the zip's central directory (entry names, stored uncompressed) is read, found from the end-of-directory
 * record in the last 64 KB; the whole file is searched only when that record isn't there. */
export function hasCharts(bytes: Uint8Array): boolean {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let names = bytes;
  for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 65_557); i--) {
    if (view.getUint32(i, true) !== 0x06054b50) continue; // the end-of-central-directory signature
    const size = view.getUint32(i + 12, true);
    const offset = view.getUint32(i + 16, true);
    if (offset + size <= bytes.length) names = bytes.subarray(offset, offset + size);
    break;
  }
  return new TextDecoder("latin1").decode(names).includes("xl/charts/chart");
}

/** Opens a web link from a document in the default browser, never in the app's window. Rejects when the backend
 * refuses it (only http, https and mailto). In the desktop app the shell also routes any other navigation or new
 * window to the backend (src-tauri/src/lib.rs). */
export async function openLink(url: string): Promise<void> {
  if (inDesktopApp) await api.openLink(url);
  else window.open(url, "_blank", "noopener,noreferrer");
}

export interface PreviewState {
  current: PreviewTarget | null;
  show: (target: PreviewTarget | null) => void;
}

export const PreviewContext = createContext<PreviewState | null>(null);

/** The session's preview pane, or null outside a session view (then file rows open the file instead). */
export function usePreview(): PreviewState | null {
  return useContext(PreviewContext);
}
