// Files attached to messages (backend/app/files/attachments.py). The backend checks every upload again; these checks
// only give a quick answer in the composer before anything is sent.
import { useEffect, useState } from "react";
import { api } from "./api";
import { apiFetch } from "./connection";

export type AttachmentKind = "image" | "pdf" | "word" | "excel" | "csv" | "text";

/** A stored attachment, as the upload returns it and the `user_message` event keeps it. */
export interface Attachment {
  id: string;
  name: string;
  mime: string;
  size: number;
  path: string;
  // The hub's plain-text copy of a PDF, Word, Excel or CSV file, when one could be made.
  text_path: string | null;
  kind: AttachmentKind;
  pages?: number | null;
  // Why there is no text copy.
  note?: string | null;
}

const MB = 1024 * 1024;
// Mirrors SIZE_CAPS in backend/app/files/attachments.py.
export const SIZE_CAPS: Record<AttachmentKind, number> = {
  image: 20 * MB,
  pdf: 32 * MB,
  word: 25 * MB,
  excel: 25 * MB,
  csv: 10 * MB,
  text: 5 * MB,
};

export const KIND_NAMES: Record<AttachmentKind, string> = {
  image: "Image",
  pdf: "PDF",
  word: "Word",
  excel: "Excel",
  csv: "CSV",
  text: "Text",
};

const DOCUMENTS: Record<string, AttachmentKind> = {
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  pdf: "pdf",
  docx: "word",
  xlsx: "excel",
  xls: "excel",
  csv: "csv",
};
// Mirrors TEXT_EXTENSIONS and TEXT_NAMES in the backend.
const TEXT = new Set(
  (
    "txt md markdown rst log json jsonl yaml yml toml ini cfg conf env xml html htm css scss less js mjs cjs jsx ts " +
    "tsx py pyi ipynb rs go java kt kts swift c h cc cpp hpp cs fs vb rb php pl lua r scala sh bash zsh ps1 psm1 " +
    "bat cmd sql graphql proto gradle tf hcl vue svelte dart ex exs erl hs clj elm tex bib diff patch tsv csproj sln"
  ).split(" "),
);
const TEXT_NAMES = new Set(["dockerfile", "makefile", "license", "readme", ".gitignore", ".gitattributes", ".editorconfig"]);

/** The file picker's filter. */
export const ACCEPT = [...Object.keys(DOCUMENTS), ...TEXT].map((ext) => `.${ext}`).join(",");

export function kindOf(name: string): AttachmentKind | null {
  const lower = name.toLowerCase();
  const dot = lower.lastIndexOf(".");
  const ext = dot > 0 ? lower.slice(dot + 1) : "";
  if (DOCUMENTS[ext]) return DOCUMENTS[ext];
  if (TEXT.has(ext) || TEXT_NAMES.has(lower)) return "text";
  return null;
}

/** Why the file can't be attached, or null when it can. */
export function attachProblem(file: File): string | null {
  const kind = kindOf(file.name);
  if (!kind) {
    return `${file.name}: this type can't be attached. Allowed: images (PNG, JPEG, GIF, WebP), PDF, Word (.docx), Excel (.xlsx, .xls), CSV, and text or code files.`;
  }
  if (file.size === 0) return `${file.name} is empty.`;
  if (file.size > SIZE_CAPS[kind]) return `${file.name} is larger than the ${SIZE_CAPS[kind] / MB} MB allowed for ${KIND_NAMES[kind]} files.`;
  return null;
}

export function sizeText(size: number): string {
  return size >= MB ? `${(size / MB).toFixed(1)} MB` : `${Math.max(1, Math.round(size / 1024))} KB`;
}

/** A pasted screenshot arrives as "image.png"; give it a name that says what and when. */
export function namePasted(file: File): File {
  if (file.name && file.name !== "image.png") return file;
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
  const ext = file.type.split("/")[1]?.replace("jpeg", "jpg") || "png";
  return new File([file], `screenshot-${stamp}.${ext}`, { type: file.type });
}

export function fileUrl(sessionId: string, id: string, text = false): string {
  return `/api/sessions/${sessionId}/attachments/${id}/file${text ? "?text=true" : ""}`;
}

/**
 * A file from the API as a Blob, loaded through apiFetch so the desktop app's token goes with it (an <img src> or
 * a viewer's own fetch can't carry the header). Null while loading; `error` when it failed. A null path loads nothing.
 * With `maxBytes`, only the first that many bytes are fetched (a Range request; the backend's file routes take it).
 */
export function useApiBlob(path: string | null, maxBytes?: number): { blob: Blob | null; error: string | null } {
  const [state, setState] = useState<{ blob: Blob | null; error: string | null }>({ blob: null, error: null });
  useEffect(() => {
    setState({ blob: null, error: null });
    if (!path) return;
    let current = true;
    apiFetch(path, maxBytes ? { headers: { Range: `bytes=0-${maxBytes - 1}` } } : {})
      .then(async (response) => {
        if (!response.ok) throw new Error((await response.json().catch(() => null))?.detail ?? `Couldn't load the file (${response.status}).`);
        return response.blob();
      })
      .then((blob) => current && setState({ blob, error: null }))
      .catch((e) => current && setState({ blob: null, error: e instanceof Error ? e.message : String(e) }));
    return () => {
      current = false;
    };
  }, [path, maxBytes]);
  return state;
}

/** An object URL for a Blob, revoked when the Blob changes or the component goes. */
export function useObjectUrl(blob: Blob | null): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!blob) return setUrl(null);
    const made = URL.createObjectURL(blob);
    setUrl(made);
    return () => URL.revokeObjectURL(made);
  }, [blob]);
  return url;
}

/** An object URL for an attachment's file (see useApiBlob). Null while loading or when it failed. */
export function useAttachmentUrl(sessionId: string, id: string): string | null {
  return useObjectUrl(useApiBlob(fileUrl(sessionId, id)).blob);
}

/** Reads the attachments out of a `user_message` event's payload. */
export function messageAttachments(payload: Record<string, any>): Attachment[] {
  return Array.isArray(payload.attachments) ? (payload.attachments as Attachment[]) : [];
}

/**
 * Uploads the files, then sends the message naming them. If an upload or the send fails, the files uploaded for
 * it are discarded again (the backend keeps a file only once a message has used it).
 */
export async function sendWithFiles(sessionId: string, text: string, files: File[]): Promise<void> {
  const uploaded: Attachment[] = [];
  try {
    for (const file of files) uploaded.push(await api.uploadAttachment(sessionId, file));
    await api.sendMessage(sessionId, text, uploaded.map((a) => a.id));
  } catch (error) {
    await Promise.allSettled(uploaded.map((a) => api.discardAttachment(sessionId, a.id)));
    throw error;
  }
}
