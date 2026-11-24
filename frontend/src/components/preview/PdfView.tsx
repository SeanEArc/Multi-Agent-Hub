// PDF in the preview pane: pdf.js with its worker and data bundled (no CDN). Loaded only when a preview opens (a
// lazy chunk). Every page gets a placeholder of its own size; only pages near the visible area are drawn, and a
// page's canvas is released once it scrolls far away, so a long PDF never holds hundreds of bitmaps. Pages are drawn
// only: no annotation layer, so links and forms do nothing.
import { useEffect, useRef } from "react";
import * as pdfjs from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

// Served from the app's own origin by vite.config.ts (pdfjsAssets).
const asset = (folder: string) => new URL(`pdfjs/${folder}/`, document.baseURI).href;
const ASSETS = {
  cMapUrl: asset("cmaps"),
  standardFontDataUrl: asset("standard_fonts"),
  wasmUrl: asset("wasm"),
  iccUrl: asset("iccs"),
};

interface Slot {
  element: HTMLDivElement;
  page: pdfjs.PDFPageProxy;
  scale: number;
  canvas: HTMLCanvasElement | null;
  render: pdfjs.RenderTask | null;
}

export default function PdfView({ blob, onError }: { blob: Blob; onError: (message: string) => void }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = host.current!;
    let cancelled = false;
    // Its own worker; destroy() ends the document and the worker when the pane closes or shows another file.
    let task: pdfjs.PDFDocumentLoadingTask | null = null;
    const slots = new Map<Element, Slot>();

    function fail(e: unknown) {
      if (!cancelled && !(e instanceof pdfjs.RenderingCancelledException)) onError(e instanceof Error ? e.message : String(e));
    }

    function draw(slot: Slot) {
      if (slot.canvas) return;
      const ratio = window.devicePixelRatio || 1;
      const viewport = slot.page.getViewport({ scale: slot.scale * ratio });
      const canvas = document.createElement("canvas");
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      slot.element.appendChild(canvas);
      slot.canvas = canvas;
      slot.render = slot.page.render({ canvas, viewport });
      slot.render.promise.catch(fail);
    }

    function release(slot: Slot) {
      slot.render?.cancel();
      slot.render = null;
      if (slot.canvas) {
        slot.canvas.width = slot.canvas.height = 0; // frees the bitmap now, not at garbage collection
        slot.canvas.remove();
        slot.canvas = null;
      }
      slot.page.cleanup();
    }

    // Pages within about a screen above or below the visible area are drawn; the rest are released.
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const slot = slots.get(entry.target);
          if (slot) (entry.isIntersecting ? draw : release)(slot);
        }
      },
      { root: element.closest(".preview-body"), rootMargin: "100% 0px" },
    );

    void (async () => {
      try {
        const data = new Uint8Array(await blob.arrayBuffer());
        if (cancelled) return;
        task = pdfjs.getDocument({ data, ...ASSETS });
        const doc = await task.promise;
        const width = Math.max(240, element.clientWidth - 32);
        for (let n = 1; n <= doc.numPages && !cancelled; n++) {
          const page = await doc.getPage(n);
          const scale = Math.min(2, width / page.getViewport({ scale: 1 }).width);
          const size = page.getViewport({ scale });
          const slot = document.createElement("div");
          slot.className = "preview-page";
          slot.style.width = `${size.width}px`;
          slot.style.height = `${size.height}px`;
          slot.setAttribute("aria-label", `Page ${n}`);
          element.appendChild(slot);
          slots.set(slot, { element: slot, page, scale, canvas: null, render: null });
          observer.observe(slot);
        }
      } catch (e) {
        fail(e);
      }
    })();

    return () => {
      cancelled = true;
      observer.disconnect();
      for (const slot of slots.values()) release(slot);
      void task?.destroy();
      element.replaceChildren();
    };
  }, [blob]);

  return <div ref={host} className="preview-pages" />;
}
