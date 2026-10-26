// Date helpers shared by the dashboard and history.

/** The backend's times are UTC; ones read back from SQLite lose their offset, so treat those as UTC too. */
export function parseTime(value: string): Date {
  return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
}

/** "just now", "5 min ago", "3 h ago", "2 days ago", then the date. */
export function timeAgo(value: string, now = Date.now()): string {
  const seconds = Math.max(0, (now - parseTime(value).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  const days = Math.floor(seconds / 86400);
  if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;
  return parseTime(value).toLocaleDateString();
}
