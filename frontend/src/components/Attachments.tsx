import { useEffect, useState } from "react";
import { api } from "../api";
import { KIND_NAMES, kindOf, sizeText, useAttachmentUrl, type Attachment, type AttachmentKind } from "../attachments";
import { attachmentPreview, usePreview } from "../preview";

// Short labels for the file badge.
export const BADGES: Record<AttachmentKind, string> = {
  image: "IMG",
  pdf: "PDF",
  word: "DOC",
  excel: "XLS",
  csv: "CSV",
  text: "TXT",
};

/** Files picked in the composer, before sending: a thumbnail for images, removable. */
export function PendingChips({ files, onRemove, disabled }: { files: File[]; onRemove: (index: number) => void; disabled?: boolean }) {
  if (!files.length) return null;
  return (
    <div className="attach-chips" aria-label="Attached files">
      {files.map((file, index) => (
        <PendingChip key={`${file.name}-${file.size}-${file.lastModified}-${index}`} file={file} disabled={disabled} onRemove={() => onRemove(index)} />
      ))}
    </div>
  );
}

function PendingChip({ file, onRemove, disabled }: { file: File; onRemove: () => void; disabled?: boolean }) {
  const kind = kindOf(file.name) ?? "text";
  const [thumb, setThumb] = useState<string | null>(null);
  useEffect(() => {
    if (kind !== "image") return;
    const url = URL.createObjectURL(file);
    setThumb(url);
    return () => URL.revokeObjectURL(url);
  }, [file, kind]);
  return (
    <span className="attach-chip" title={`${file.name} · ${sizeText(file.size)}`}>
      {thumb ? <img className="attach-thumb" src={thumb} alt="" /> : <span className={`attach-badge kind-${kind}`}>{BADGES[kind]}</span>}
      <span className="attach-name">{file.name}</span>
      <span className="attach-size muted">{sizeText(file.size)}</span>
      <button type="button" className="attach-remove" aria-label={`Remove ${file.name}`} disabled={disabled} onClick={onRemove}>
        ×
      </button>
    </span>
  );
}

/** A sent message's files in the transcript: images inline, other files as rows that open the file. */
export function MessageAttachments({ sessionId, attachments }: { sessionId: string; attachments: Attachment[] }) {
  if (!attachments.length) return null;
  const images = attachments.filter((a) => a.kind === "image");
  const files = attachments.filter((a) => a.kind !== "image");
  return (
    <div className="message-attachments">
      {images.length > 0 && (
        <div className="attach-images">
          {images.map((a) => (
            <InlineImage key={a.id} sessionId={sessionId} attachment={a} />
          ))}
        </div>
      )}
      {files.map((a) => (
        <FileRow key={a.id} sessionId={sessionId} attachment={a} />
      ))}
    </div>
  );
}

/** Shows the file in the session's preview pane, or opens it with its own app where there is no pane. */
function useOpen(sessionId: string, attachment: Attachment) {
  const preview = usePreview();
  const [error, setError] = useState<string | null>(null);
  async function open() {
    setError(null);
    if (preview) return preview.show(attachmentPreview(sessionId, attachment));
    try {
      await api.openAttachment(sessionId, attachment.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return { open, error, previews: preview !== null };
}

function InlineImage({ sessionId, attachment }: { sessionId: string; attachment: Attachment }) {
  const url = useAttachmentUrl(sessionId, attachment.id);
  const { open, error, previews } = useOpen(sessionId, attachment);
  return (
    <button type="button" className="attach-image" title={error ?? `${attachment.name} · ${previews ? "preview" : "open full size"}`} onClick={() => void open()}>
      {url ? <img src={url} alt={attachment.name} /> : <span className="attach-image-placeholder muted">{attachment.name}</span>}
    </button>
  );
}

function FileRow({ sessionId, attachment }: { sessionId: string; attachment: Attachment }) {
  const { open, error, previews } = useOpen(sessionId, attachment);
  const details = [KIND_NAMES[attachment.kind], attachment.pages ? `${attachment.pages} page${attachment.pages === 1 ? "" : "s"}` : null, sizeText(attachment.size)]
    .filter(Boolean)
    .join(" · ");
  const copy = attachment.text_path
    ? "text copy made"
    : ["pdf", "word", "excel", "csv"].includes(attachment.kind)
      ? `no text copy${attachment.note ? `: ${attachment.note}` : ""}`
      : null;
  return (
    <button type="button" className="attach-file" title={error ?? `${previews ? "Preview" : "Open"} ${attachment.path}`} onClick={() => void open()}>
      <span className={`attach-badge kind-${attachment.kind}`}>{BADGES[attachment.kind]}</span>
      <span className="attach-file-name">{attachment.name}</span>
      <span className="muted attach-file-details">{details}</span>
      {copy && <span className={`attach-copy${attachment.text_path ? "" : " missing"}`}>{copy}</span>}
      {error && <span className="attach-error">{error}</span>}
    </button>
  );
}
