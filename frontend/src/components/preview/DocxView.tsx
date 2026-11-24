// Word in the preview pane: BetterOffice's web engine (WASM in a worker), loaded only when a preview opens (a
// lazy chunk). Nothing is fetched online: Google Fonts is off and the bundled fonts load from the app's own assets.
// The pane never edits: read-only, viewing mode, no toolbar, and tracked changes shown as stored, with cards that
// have no Accept or Reject buttons.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { setGoogleFontsEnabled } from "@betteroffice/docx";
import { configureDefaultFonts } from "@betteroffice/docx/layout";
import { DocxEditor, type DocxEditorRef, type TrackedChangeCardRenderProps } from "@betteroffice/docx-react";
import "@betteroffice/docx-react/styles.css";
import * as fonts from "@betteroffice/fonts";
import { useResolvedTheme } from "../../theme";

// Process-wide, once, before any viewer mounts (the engine's Google Fonts link defaults to on).
setGoogleFontsEnabled(false);
configureDefaultFonts({ fonts });

const CHANGE_NAMES: Record<string, string> = {
  insertion: "Inserted",
  deletion: "Deleted",
  replacement: "Replaced",
  paragraphMarkInsertion: "Paragraph added",
  paragraphMarkDeletion: "Paragraph removed",
};

/** A tracked change's card, read-only: who, what and when, no Accept or Reject. */
function ChangeCard({ change, measureRef }: TrackedChangeCardRenderProps) {
  const text = change.type === "replacement" ? `${change.deletedText ?? ""} → ${change.text}` : change.text;
  return (
    <div ref={measureRef} className="preview-change-card">
      <strong>{change.author || "Unknown author"}</strong>
      <span className="muted">
        {" "}
        · {CHANGE_NAMES[change.type] ?? "Formatting changed"}
        {change.date ? ` · ${new Date(change.date).toLocaleDateString()}` : ""}
      </span>
      {text && <div className="preview-change-text">{text.length > 160 ? `${text.slice(0, 160)}…` : text}</div>}
    </div>
  );
}

// A Letter or A4 page at 100% with the viewer's margins around it, and the tracked-change cards' column beside it.
const PAGE_PX = 900;
const CARDS_PX = 300;

/** The zoom that fits the page (and the cards' column, when shown) into a pane this wide, leaving room for the
 * viewer's own vertical scrollbar and the change dots. */
function fit(width: number, cards: boolean): number {
  return Math.max(0.35, Math.min(1, (width - (cards ? CARDS_PX : 64)) / PAGE_PX));
}

export default function DocxView({ blob, name, onError }: { blob: Blob; name: string; onError: (message: string) => void }) {
  const box = useRef<HTMLDivElement>(null);
  const editor = useRef<DocxEditorRef>(null);
  const theme = useResolvedTheme();
  // Measured before the viewer mounts, so it opens fitted; cards beside the page when there is room, else dots
  // in the margin (chosen once: the viewer takes it at mount).
  const [layout, setLayout] = useState<{ width: number; cards: boolean } | null>(null);
  useLayoutEffect(() => {
    const width = box.current!.clientWidth;
    setLayout({ width, cards: width >= PAGE_PX * 0.6 + CARDS_PX });
  }, []);
  // Refit when the pane changes width (window resized, split layout toggled).
  const cards = layout?.cards ?? false;
  useEffect(() => {
    if (!layout) return;
    const observer = new ResizeObserver(([entry]) => editor.current?.setZoom(fit(entry.contentRect.width, cards)));
    observer.observe(box.current!);
    return () => observer.disconnect();
  }, [layout !== null, cards]);

  return (
    <div ref={box} className="preview-docx">
      {layout && (
        <DocxEditor
          ref={editor}
          documentBuffer={blob}
          documentName={name}
          readOnly
          mode="viewing"
          showToolbar={false}
          showFileOpen={false}
          showOutlineButton={false}
          downloadOnSave={false}
          initialZoom={fit(layout.width, cards)}
          colorMode={theme}
          trackedChanges={{ display: cards ? "cards" : "dots", renderCard: ChangeCard }}
          onError={(error, info) => {
            if (!info?.recoverable) onError(error.message);
          }}
        />
      )}
    </div>
  );
}
