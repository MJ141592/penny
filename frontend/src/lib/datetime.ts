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
