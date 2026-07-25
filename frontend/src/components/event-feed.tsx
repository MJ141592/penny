/**
 * The history: day dividers and the cards under them.
 *
 * Grouping is `groupByDay`, which walks the already-sorted list in the household's timezone and
 * starts a new group when the day key changes. It does not bucket, so a response that came back
 * mis-sorted shows up as a repeated divider instead of being silently tidied into looking right.
 *
 * A failed refetch renders the error *above* whatever is already on screen rather than replacing
 * it: a family reading last week's appointments should not lose them because the poll timed out.
 */

import { groupByDay } from '../lib/datetime'
import type { Event } from '../types/api'
import { DayDivider } from './day-divider'
import { FeedEmptyState } from './empty-state'
import { FeedCard } from './feed-card'
import styles from './event-feed.module.css'
import { ErrorState, FeedSkeleton } from './query-state'

export interface EventFeedProps {
  events: Event[]
  timeZone: string
  isPending?: boolean
  error?: unknown
  onRetry?: () => void
  /** Total messages ingested, which is what tells "found nothing" from "imported nothing". */
  messageCount?: number | undefined
}

export function EventFeed({
  events,
  timeZone,
  isPending,
  error,
  onRetry,
  messageCount,
}: EventFeedProps) {
  const days = groupByDay(events, timeZone, (event) => event.occurred_at)
  const showEmpty = !isPending && !error && events.length === 0

  return (
    <div className={styles.feed}>
      {error ? (
        <ErrorState error={error} title="Couldn't load the history" onRetry={onRetry} />
      ) : null}
      {isPending && events.length === 0 ? <FeedSkeleton /> : null}
      {showEmpty ? <FeedEmptyState messageCount={messageCount} /> : null}

      {days.map(({ key, items }) => {
        const first = items[0]
        if (!first) return null
        return (
          <section key={key} className={styles.day}>
            <DayDivider iso={first.occurred_at} timeZone={timeZone} count={items.length} />
            <ol className={styles.list}>
              {items.map((event) => (
                <li key={event.id}>
                  <FeedCard event={event} timeZone={timeZone} />
                </li>
              ))}
            </ol>
          </section>
        )
      })}
    </div>
  )
}
