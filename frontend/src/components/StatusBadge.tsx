import type { SessionStatus } from "../api";

const LABELS: Record<SessionStatus, string> = {
  ready: "Ready",
  working: "Working",
  handing_off: "Handing off",
  waiting_for_input: "Needs input",
  completed: "Completed",
  failed: "Failed",
};

export function StatusDot({ status }: { status: SessionStatus }) {
  return <span className={`status-dot status-${status}`} aria-hidden="true" />;
}

export function StatusBadge({ status }: { status: SessionStatus }) {
  return (
    <span className={`status-badge status-${status}`}>
      <StatusDot status={status} />
      {LABELS[status]}
    </span>
  );
}
