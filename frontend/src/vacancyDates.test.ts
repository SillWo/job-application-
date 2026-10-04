import { formatUtcTimestampLocal, localDateTimeBounds } from "./vacancyDates";

afterEach(() => vi.unstubAllEnvs());

test.each([
  ["Europe/Moscow", "2026-01-01", "2025-12-31T21:00:00.000Z", "2026-01-01T21:00:00.000Z"],
  ["Pacific/Honolulu", "2026-01-01", "2026-01-01T10:00:00.000Z", "2026-01-02T10:00:00.000Z"],
  // The selected local day is 23 hours long because daylight saving starts.
  ["America/Los_Angeles", "2026-03-08", "2026-03-08T08:00:00.000Z", "2026-03-09T07:00:00.000Z"],
])("uses local-day bounds in %s, including timezone offset and DST", (timeZone, day, from, before) => {
  vi.stubEnv("TZ", timeZone);
  expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe(timeZone);
  expect(localDateTimeBounds(day, day)).toEqual({ from, before });
});

test("formats API UTC timestamps in the user's local timezone", () => {
  vi.stubEnv("TZ", "America/Los_Angeles");
  const formatted = formatUtcTimestampLocal("2026-09-29T02:13:00Z");
  expect(formatted).toContain("28.09.2026");
  expect(formatted).toContain("19:13");
});

test("invalid date filters and timestamps are safely ignored or preserved", () => {
  vi.stubEnv("TZ", "Europe/Moscow");
  expect(() => localDateTimeBounds("2026-02-30", "not-a-date")).not.toThrow();
  expect(localDateTimeBounds("2026-02-30", "not-a-date")).toEqual({});
  expect(formatUtcTimestampLocal("not-a-timestamp")).toBe("not-a-timestamp");
});
