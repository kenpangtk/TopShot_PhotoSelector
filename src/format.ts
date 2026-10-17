export function formatBytes(bytes: number | null | undefined): string {
  if (!bytes || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  const value = bytes / Math.pow(1024, index);
  return `${value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}

export function formatNumber(value: number | null | undefined): string {
  return new Intl.NumberFormat("en-US").format(value ?? 0);
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  if (minutes < 60) return `${minutes}m ${rest}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function shortPath(path: string | null | undefined, keep = 3): string {
  if (!path) return "—";
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= keep) return path;
  return `…/${parts.slice(-keep).join("/")}`;
}

export function basename(path: string | null | undefined): string {
  if (!path) return "—";
  return path.split("/").filter(Boolean).pop() ?? path;
}

export function stateLabel(state: string): string {
  if (state === "keep") return "Keep";
  if (state === "reject") return "Reject";
  return "Review";
}

export function suggestionLabel(suggestion: string): string {
  if (suggestion === "alternate") return "Alternate";
  if (suggestion === "keep") return "Keep";
  if (suggestion === "reject") return "Reject";
  return "Review";
}
