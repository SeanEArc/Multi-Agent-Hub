import { useEffect, useState } from "react";
import { api, type SessionEvent, type SessionStatus } from "./api";

export interface SessionStream {
  events: SessionEvent[];
  status: SessionStatus | null;
  connected: boolean;
}

/** Follows a session's server-sent event stream: full history first, then live updates. */
export function useSessionStream(sessionId: string | null): SessionStream {
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const [status, setStatus] = useState<SessionStatus | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    setEvents([]);
    setStatus(null);
    setConnected(false);
    if (!sessionId) return;

    // EventSource reconnects on its own and sends Last-Event-ID, so the backend
    // resumes after the last event we received.
    const source = new EventSource(api.streamUrl(sessionId));
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.addEventListener("status", (message) => {
      setStatus(JSON.parse(message.data).status);
    });
    source.addEventListener("event", (message) => {
      const event: SessionEvent = JSON.parse(message.data);
      setEvents((previous) =>
        previous.length && previous[previous.length - 1].id >= event.id ? previous : [...previous, event],
      );
    });
    return () => source.close();
  }, [sessionId]);

  return { events, status, connected };
}
