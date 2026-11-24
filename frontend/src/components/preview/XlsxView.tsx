// Excel (.xlsx) in the preview pane: BetterOffice's web engine (WASM in a worker), loaded only when a preview
// opens (a lazy chunk). Read-only, no toolbar. Charts are drawn as blank boxes, so a workbook with charts gets a
// note with Open in Excel above the grid.
import { useEffect, useState } from "react";
import { XlsxEditor } from "@betteroffice/xlsx-react";
import { hasCharts } from "../../preview";

interface Props {
  blob: Blob;
  name: string;
  onError: (message: string) => void;
  onOpen: () => void;
}

export default function XlsxView({ blob, name, onError, onOpen }: Props) {
  const [file, setFile] = useState<{ bytes: Uint8Array; charts: boolean } | null>(null);

  useEffect(() => {
    let current = true;
    blob.arrayBuffer().then(
      (buffer) => {
        const bytes = new Uint8Array(buffer);
        // Before the editor gets the bytes (its worker may take them).
        if (current) setFile({ bytes, charts: hasCharts(bytes) });
      },
      (e) => current && onError(e instanceof Error ? e.message : String(e)),
    );
    return () => {
      current = false;
    };
  }, [blob]);

  if (!file) return <div className="preview-status muted">Loading…</div>;
  return (
    <div className="preview-xlsx">
      {file.charts && (
        <div className="preview-note">
          This workbook has charts, which the preview can't draw (they show as blank boxes).
          <button type="button" className="button subtle" onClick={onOpen}>
            Open in Excel
          </button>
        </div>
      )}
      <div className="preview-grid">
        <XlsxEditor file={file.bytes} fileName={name} readOnly showToolbar={false} onError={(error) => onError(error.message)} />
      </div>
    </div>
  );
}
