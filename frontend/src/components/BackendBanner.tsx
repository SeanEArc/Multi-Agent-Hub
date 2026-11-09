import { useEffect, useState } from "react";
import { type BackendStatus, onBackendStatus, restartBackend } from "../connection";

/** Desktop app only: what the shell reports about the backend it runs. */
export function BackendBanner({ unreachable }: { unreachable: boolean }) {
  const [status, setStatus] = useState<BackendStatus | null>(null);
  useEffect(() => onBackendStatus(setStatus), []);

  if (!status) return null;
  if (status.state === "failed") {
    return (
      <div className="banner error">
        {status.message ?? "The backend stopped."}{" "}
        <button type="button" className="banner-action" onClick={() => void restartBackend()}>
          Retry
        </button>
      </div>
    );
  }
  if (status.state === "starting" || status.state === "restarting") {
    return <div className="banner">{status.message ?? "Starting the backend…"}</div>;
  }
  if (status.state === "ready" && unreachable) {
    return <div className="banner error">Can't reach the backend.</div>;
  }
  return null;
}
