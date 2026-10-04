function parseLocalDate(value: string, nextDay = false): Date | null {
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})$/u);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return null;
  if (nextDay) date.setDate(date.getDate() + 1);
  return date;
}

/** Convert date-only filters to local-midnight UTC boundaries for the API. */
export function localDateTimeBounds(from: string, to: string): { from?: string; before?: string } {
  const fromDate = from ? parseLocalDate(from) : null;
  const toDate = to ? parseLocalDate(to, true) : null;
  return {
    ...(fromDate ? { from: fromDate.toISOString() } : {}),
    ...(toDate ? { before: toDate.toISOString() } : {}),
  };
}

/** API timestamps are UTC, including legacy values without an explicit offset. */
export function formatUtcTimestampLocal(value: string): string {
  if (!value) return value;
  const candidate = /(?:Z|[+-]\d{2}:?\d{2})$/u.test(value) ? value : `${value}Z`;
  const parsed = new Date(candidate);
  if (Number.isNaN(parsed.getTime())) return value;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(parsed);
}
