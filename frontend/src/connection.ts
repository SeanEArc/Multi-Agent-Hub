// How the UI reaches the backend.
//
// In a browser (dev), the UI calls /api on its own origin and the Vite proxy adds the token, so
// the page never holds it. In the desktop app, the Tauri shell starts the backend on a free port
// with a fresh token and hands both to the UI through the `hub_connection` command; the UI then
// adds the Authorization header itself. EventSource can't send headers, so the desktop app reads
// the event stream with fetch instead.

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

interface Connection {
  baseUrl: string; // "" in the browser, so paths stay relative to the page
  token: string | null;
}

export const inDesktopApp = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

let connection: Promise<Connection> | null = null;

function getConnection(): Promise<Connection> {
  connection ??= inDesktopApp ? desktopConnection() : Promise.resolve({ baseUrl: "", token: null });
  return connection;
}

// The first requests wait for the shell to report the backend ready, so the UI's first load
// doesn't fail while the backend is still starting. Later restarts aren't waited for: polling
// and the event stream recover on their own.
async function desktopConnection(): Promise<Connection> {
  const c = await invoke<{ base_url: string; token: string }>("hub_connection");
  await new Promise<void>((resolve) => {
    const done = onBackendStatus((status) => {
      if (status.state === "ready" || status.state === "failed") {
        done();
        resolve();
      }
    });
  });
  return { baseUrl: c.base_url, token: c.token };
}

export type BackendState = "starting" | "ready" | "restarting" | "failed" | "stopped";

export interface BackendStatus {
  state: BackendState;
  message: string | null;
}

/** Calls `handler` with the shell's backend status now and on every change. Returns an unsubscribe. */
export function onBackendStatus(handler: (status: BackendStatus) => void): () => void {
  if (!inDesktopApp) return () => undefined;
  let active = true;
  const unlisten = listen<BackendStatus>("backend-status", (event) => active && handler(event.payload));
  void invoke<BackendStatus>("backend_status").then((status) => active && handler(status));
  return () => {
    active = false;
    void unlisten.then((stop) => stop());
  };
}

export function restartBackend(): Promise<void> {
  return invoke("restart_backend");
}

/** fetch() for an API path such as "/api/sessions", with the desktop app's base URL and token. */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const { baseUrl, token } = await getConnection();
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(`${baseUrl}${path}`, { ...init, headers });
}

export interface StreamMessage {
  data: string;
}

/** The parts of EventSource the UI uses. */
export interface EventStream {
  onopen: (() => void) | null;
  onerror: (() => void) | null;
  addEventListener(type: string, listener: (message: StreamMessage) => void): void;
  close(): void;
}

/** Follows a server-sent event stream: EventSource in the browser, fetch in the desktop app. */
export function openEventStream(path: string): EventStream {
  // EventSource's handlers take an Event argument the UI ignores; otherwise it matches.
  if (!inDesktopApp) return new EventSource(path) as unknown as EventStream;
  return new FetchEventStream(path);
}

const RETRY_MS = 1500;

class FetchEventStream implements EventStream {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, Array<(message: StreamMessage) => void>>();
  private lastEventId: string | null = null;
  private abort = new AbortController();
  private closed = false;

  constructor(private readonly path: string) {
    void this.run();
  }

  addEventListener(type: string, listener: (message: StreamMessage) => void): void {
    const list = this.listeners.get(type) ?? [];
    list.push(listener);
    this.listeners.set(type, list);
  }

  close(): void {
    this.closed = true;
    this.abort.abort();
  }

  // Reconnects like EventSource: after a drop (or the backend restarting), wait and resume after
  // the last event received, using Last-Event-ID.
  private async run(): Promise<void> {
    while (!this.closed) {
      try {
        const headers: Record<string, string> = { Accept: "text/event-stream" };
        if (this.lastEventId !== null) headers["Last-Event-ID"] = this.lastEventId;
        const response = await apiFetch(this.path, { headers, signal: this.abort.signal });
        if (!response.ok || !response.body) throw new Error(`stream failed (${response.status})`);
        this.onopen?.();
        await this.read(response.body);
      } catch {
        if (this.closed) return;
      }
      if (this.closed) return;
      this.onerror?.();
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
    }
  }

  private async read(body: ReadableStream<Uint8Array>): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, "\n");
      let end: number;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        this.dispatch(buffer.slice(0, end));
        buffer = buffer.slice(end + 2);
      }
    }
  }

  private dispatch(block: string): void {
    let type = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue; // comments, such as the backend's pings
      const colon = line.indexOf(":");
      const field = colon < 0 ? line : line.slice(0, colon);
      const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") type = value;
      else if (field === "data") data.push(value);
      else if (field === "id") this.lastEventId = value;
    }
    if (!data.length) return;
    const message = { data: data.join("\n") };
    for (const listener of this.listeners.get(type) ?? []) listener(message);
  }
}
