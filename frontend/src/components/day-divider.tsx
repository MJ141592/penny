/**
 * The "Today" / "Friday, 17 July 2026" heading between runs of events.
 *
 * It takes the household's timezone, never the browser's, and does the whole calculation through
 * `Intl` with an explicit `timeZone`. A symptom logged at 23:40 in London has to sit under Friday
 * for the sibling reading it in Dubai at 02:40 on Saturday — otherwise two people looking at the
 * same feed disagree about the day something happened, which is the one thing this product exists
 * to settle.
 */

import { dayKey, formatDayHeading } from '../lib/datetime'
import styles from './day-divider.module.css'

export interface DayDividerProps {
  /** ISO UTC instant of any event in the day. */
  iso: string
  timeZone: string
  count: number
}

export function DayDivider({ iso, timeZone, count }: DayDividerProps) {
  return (
    <div className={styles.divider}>
      <h2 className={styles.heading}>
        <time dateTime={dayKey(iso, timeZone)}>{formatDayHeading(iso, timeZone)}</time>
      </h2>
      <span className={styles.count}>{count === 1 ? '1 entry' : `${count} entries`}</span>
    </div>
  )
}
