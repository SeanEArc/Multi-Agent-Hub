// The preview pane beside the chat (revision 4, attachments PR 3): an attachment or an agent's output file, shown
// as it is, never edited. PDF, Word and Excel engines are lazy chunks, so their WebAssembly and fonts load only
// when a preview opens, and unmount (releasing their worker) when the pane closes or shows another file.
import { Component, Suspense, lazy, useEffect, useState, type MouseEvent, type ReactNode } from "react";
import { useApiBlob, useObjectUrl } from "../attachments";
import { engineFor, openLink, type PreviewTarget } from "../preview";

const PdfView = lazy(() => import("./preview/PdfView"));
const DocxView = lazy(() => import("./preview/DocxView"));
const XlsxView = lazy(() => import("./preview/XlsxView"));

// A text file is shown up to this many bytes: a larger one (an agent's 600 MB log) is fetched only this far, with
// a Range request, so it is never downloaded or decoded whole.
const TEXT_BYTES = 4 * 1024 * 1024;
// Files the PDF, Word, Excel and image viewers are given at most (attachments are capped at 32 MB; outputs aren't).
const VIEWER_BYTES = 64 * 1024 * 1024;

interface Props {
  target: PreviewTarget;
  onClose: () => void;
}

/** Keyed by the target's version: another file, or an overwritten output, starts afresh (new fetch, viewer
 * remounted, no error left from the last one). */
export function PreviewPane(props: Props) {
  return <Pane key={props.target.version} {...props} />;
}

function Pane({ target, onClose }: Props) {
  const [error, setError] = useState<string | null>(null);
  async function open() {
    setError(null);
    try {
      await target.open();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  // A link inside a document opens in the browser; the app's window never navigates away.
  function catchLinks(e: MouseEvent) {
    const href = (e.target as HTMLElement).closest?.("a[href]")?.getAttribute("href");
    if (!href || href.startsWith("#")) return; // a place inside the document
    e.preventDefault();
    setError(null);
    openLink(href).catch((err) => setError(`That link wasn't opened: ${err instanceof Error ? err.message : String(err)}`));
  }

  return (
    <aside className="preview-pane" aria-label={`Preview of ${target.name}`} onClickCapture={catchLinks}>
      <header className="preview-header">
        <span className="preview-name" title={target.name}>
          {target.name}
        </span>
        <button type="button" className="button subtle" onClick={() => void open()} title="Open with its own app">
          Open
        </button>
        <button type="button" className="button subtle" onClick={onClose} aria-label="Close the preview">
          Close
        </button>
      </header>
      {error && <div className="preview-error">{error}</div>}
      <PreviewBody target={target} onOpen={() => void open()} />
    </aside>
  );
}

function PreviewBody({ target, onOpen }: { target: PreviewTarget; onOpen: () => void }) {
  const engine = engineFor(target.name);
  const tooLarge = engine !== null && engine !== "text" && target.size > VIEWER_BYTES;
  const [failed, setFailed] = useState<string | null>(null);
  const { blob, error } = useApiBlob(engine && !tooLarge ? target.url : null, engine === "text" ? TEXT_BYTES : undefined);

  if (!engine || tooLarge || failed || error) {
    const reason = failed
      ? `The preview couldn't open this file (${failed}).`
      : error
        ? error
        : tooLarge
          ? `This file is too large to preview here (over ${VIEWER_BYTES / 1024 / 1024} MB).`
          : target.name.toLowerCase().endsWith(".xls")
            ? "The preview reads .xlsx workbooks, not the older .xls format."
            : "This type can't be previewed here.";
    return <Fallback target={target} reason={reason} onOpen={onOpen} />;
  }
  if (!blob) return <div className="preview-status muted">Loading…</div>;
  const view = (() => {
    switch (engine) {
      case "image":
        return <ImageView blob={blob} name={target.name} />;
      case "text":
        return <TextView blob={blob} />;
      case "pdf":
        return <PdfView blob={blob} onError={setFailed} />;
      case "word":
        return <DocxView blob={blob} name={target.name} onError={setFailed} />;
      case "excel":
        return <XlsxView blob={blob} name={target.name} onError={setFailed} onOpen={onOpen} />;
    }
  })();
  return (
    <div className="preview-body">
      <Catch onError={setFailed}>
        <Suspense fallback={<div className="preview-status muted">Loading the viewer…</div>}>{view}</Suspense>
      </Catch>
    </div>
  );
}

function ImageView({ blob, name }: { blob: Blob; name: string }) {
  const url = useObjectUrl(blob);
  return url ? <img className="preview-image" src={url} alt={name} /> : null;
}

function TextView({ blob }: { blob: Blob }) {
  const [text, setText] = useState<string | null>(null);
  useEffect(() => {
    let current = true;
    void blob.text().then((value) => current && setText(value));
    return () => {
      current = false;
    };
  }, [blob]);
  if (text === null) return <div className="preview-status muted">Loading…</div>;
  return (
    <>
      <pre className="preview-text">{text}</pre>
      {blob.size >= TEXT_BYTES && <div className="preview-status muted">Only the first {TEXT_BYTES / 1024 / 1024} MB are shown. Open the file to see it all.</div>}
    </>
  );
}

/** When no engine can show the file: the hub's text copy when there is one, and Open. */
function Fallback({ target, reason, onOpen }: { target: PreviewTarget; reason: string; onOpen: () => void }) {
  const { blob, error } = useApiBlob(target.textUrl, TEXT_BYTES);
  return (
    <div className="preview-body">
      <div className="preview-note">
        {reason} {target.textUrl ? "Showing the hub's text copy." : ""}
        <button type="button" className="button subtle" onClick={onOpen}>
          Open
        </button>
      </div>
      {target.textUrl && (error ? <div className="preview-status muted">{error}</div> : blob ? <TextView blob={blob} /> : null)}
    </div>
  );
}

/** A viewer that throws (or a chunk that won't load) shows the fallback instead of breaking the session view. */
class Catch extends Component<{ onError: (message: string) => void; children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: unknown) {
    this.props.onError(error instanceof Error ? error.message : String(error));
  }
  render() {
    return this.state.failed ? null : this.props.children;
  }
}
