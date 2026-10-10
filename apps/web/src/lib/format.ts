/** Presentation helpers only: no domain logic lives here. */

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID_PATTERN.test(value);
}

export function shortSha(sha: string): string {
  return sha.slice(0, 10);
}

const integer = new Intl.NumberFormat("en-US");

export function formatCount(value: number): string {
  return integer.format(value);
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
}

/** UTC, fixed format, so server and client renders always agree. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "not recorded";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** "analysis_reference" -> "analysis reference". */
export function humanize(value: string): string {
  return value.replace(/_/g, " ");
}

export function lineRange(start: number | null, end: number | null): string {
  if (start === null) return "";
  if (end === null || end === start) return `:${start}`;
  return `:${start}-${end}`;
}
