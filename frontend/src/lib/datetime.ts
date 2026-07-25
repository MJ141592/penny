/**
 * Every date the user sees is rendered in the household's timezone, never the browser's.
 *
 * The API sends UTC with a trailing `Z` and the household carries an IANA name, so a sibling in
 * Sydney and a sibling in Salford must see the same day divider over the same event. `Intl` does
 * the whole job; there is no date library in this project.
 */

const formatters = new Map<string, Intl.DateTimeFormat>()

function formatter(timeZone: string, options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = `${timeZone}|${JSON.stringify(options)}`
  let cached = formatters.get(key)
  if (!cached) {
    cached = new Intl.DateTimeFormat('en-GB', { timeZone, ...options })
    formatters.set(key, cached)
  }
  return cached
}

/**
 * `YYYY-MM-DD` in the household timezone — the grouping key for day dividers. Built from parts
 * rather than a locale pattern so it cannot drift with ICU's idea of `en-GB`.
 */
export function dayKey(iso: string, timeZone: string): string {
  const parts = formatter(timeZone, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date(iso))
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((candidate) => candidate.type === type)?.value ?? ''
  return `${part('year')}-${part('month')}-${part('day')}`
}

export function formatTime(iso: string, timeZone: string): string {
  return formatter(timeZone, { hour: '2-digit', minute: '2-digit', hour12: false }).format(
    new Date(iso),
  )
}

/** "Tue 14 Jul 2026, 21:04" — the compact stamp the import wizard checks its guess against. */
export function formatEvidenceStamp(iso: string, timeZone: string): string {
  return formatter(timeZone, {
    weekday: 'short',
    day: '2-digit',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(iso))
}

/* ------------------------------------------------------- editing a date in someone else's zone */

/**
 * How far ahead of UTC `timeZone` was at instant `ts`, in milliseconds.
 *
 * There is no API for this, so it is read back out of a formatted string: format the instant in
 * the zone, reinterpret those wall-clock digits as if they were UTC, and the difference is the
 * offset. Handles DST because it asks about one specific instant, not about the zone in general.
 */
function zoneOffsetMs(ts: number, timeZone: string): number {
  const parts = formatter(timeZone, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(new Date(ts))
  const num = (type: Intl.DateTimeFormatPartTypes) =>
    Number(parts.find((candidate) => candidate.type === type)?.value ?? '0')
  // `hour12: false` renders midnight as 24 in some ICU versions; Date.UTC normalises it anyway.
  const asIfUtc = Date.UTC(num('year'), num('month') - 1, num('day'), num('hour'), num('minute'), num('second'))
  return asIfUtc - ts
}

/** `YYYY-MM-DDTHH:mm` in the household's zone — the value an `<input type="datetime-local">` wants. */
export function toLocalInputValue(iso: string, timeZone: string): string {
  return `${dayKey(iso, timeZone)}T${formatTime(iso, timeZone)}`
}

/**
 * The inverse: wall-clock digits the user typed, read as a time in the HOUSEHOLD's zone, back to
 * a UTC instant. Never the browser's zone — a daughter editing from Dubai must not move an
 * appointment four hours when she fixes its title.
 *
 * Solved by iteration because the offset depends on the answer: guess the instant, ask what the
 * offset was there, correct, and ask again. Two passes settle it everywhere except the one hour a
 * year that a DST jump makes genuinely ambiguous, where either answer is defensible.
 */
export function fromLocalInputValue(local: string, timeZone: string): string | null {
  // Some browsers hand back seconds as well; only minutes are ever offered, so trim to them.
  const asIfUtc = Date.parse(`${local.slice(0, 16)}:00Z`)
  if (Number.isNaN(asIfUtc)) return null
  let ts = asIfUtc
  for (let pass = 0; pass < 2; pass += 1) ts = asIfUtc - zoneOffsetMs(ts, timeZone)
  return new Date(ts).toISOString()
}

export function formatDate(iso: string, timeZone: string): string {
  return formatter(timeZone, { day: 'numeric', month: 'long', year: 'numeric' }).format(
    new Date(iso),
  )
}

export function formatMonth(iso: string, timeZone: string): string {
  return formatter(timeZone, { month: 'long', year: 'numeric' }).format(new Date(iso))
}

/** "Today" / "Yesterday" / "Friday, 17 July 2026" — the day divider heading. */
export function formatDayHeading(iso: string, timeZone: string, now = new Date()): string {
  const key = dayKey(iso, timeZone)
  if (key === dayKey(now.toISOString(), timeZone)) return 'Today'
  const yesterday = new Date(now.getTime() - 86_400_000)
  if (key === dayKey(yesterday.toISOString(), timeZone)) return 'Yesterday'
  return formatter(timeZone, {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  }).format(new Date(iso))
}

export interface DayGroup<T> {
  key: string
  heading: string
  items: T[]
}

/**
 * Groups an already-sorted list into consecutive runs sharing a day. Consecutive, not bucketed:
 * the API guarantees the order, so preserving it costs nothing and a mis-sorted response shows
 * up as a repeated divider rather than being silently reordered into looking correct.
 */
export function groupByDay<T>(
  items: T[],
  timeZone: string,
  at: (item: T) => string,
  now = new Date(),
): DayGroup<T>[] {
  const groups: DayGroup<T>[] = []
  for (const item of items) {
    const key = dayKey(at(item), timeZone)
    const last = groups.at(-1)
    if (last && last.key === key) {
      last.items.push(item)
    } else {
      groups.push({ key, heading: formatDayHeading(at(item), timeZone, now), items: [item] })
    }
  }
  return groups
}

/** The Monday-anchored week label used when precision is `"week"`. */
function formatWeek(iso: string, timeZone: string): string {
  return `Week of ${formatter(timeZone, { day: 'numeric', month: 'long' }).format(new Date(iso))}`
}

/**
 * Renders only as much of the timestamp as the precision justifies. Showing "09:30" on an event
 * the model only placed to the month invents a certainty the source message never had.
 */
export function formatOccurredAt(
  iso: string,
  precision: 'exact' | 'day' | 'week' | 'month' | 'unknown',
  timeZone: string,
): string {
  switch (precision) {
    case 'exact':
      return `${formatDate(iso, timeZone)}, ${formatTime(iso, timeZone)}`
    case 'day':
      return formatDate(iso, timeZone)
    case 'week':
      return formatWeek(iso, timeZone)
    case 'month':
      return formatMonth(iso, timeZone)
    case 'unknown':
      return 'Date uncertain'
  }
}
