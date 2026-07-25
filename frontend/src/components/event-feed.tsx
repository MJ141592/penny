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

import { useState } from 'react'

import { useDeleteEvent, useUpdateEvent } from '../api/queries'
import { groupByDay } from '../lib/datetime'
import type { Event } from '../types/api'
import { DayDivider } from './day-divider'
import { FeedEmptyState } from './empty-state'
import { EventEditor } from './event-editor'
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
  /** Off by default: a read-only surface should not grow edit buttons by accident. */
  editable?: boolean
}

export function EventFeed({
  events,
  timeZone,
  isPending,
  error,
  onRetry,
  messageCount,
  editable = false,
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
                  <FeedRow event={event} timeZone={timeZone} editable={editable} />
                </li>
              ))}
            </ol>
          </section>
        )
      })}
    </div>
  )
}

/**
 * One row: the card, or the card opened for editing.
 *
 * The open/closed flag lives per row rather than as one `editingId` threaded down from the route,
 * so that a poll landing mid-edit re-renders the list around this component without closing the
 * form somebody is halfway through typing into.
 *
 * Both mutations write to the cache before the request goes out and roll back if it fails, so a
 * save reads as instant and a failure reads as "it didn't save" — never as a silent no-op.
 */
function FeedRow({
  event,
  timeZone,
  editable,
}: {
  event: Event
  timeZone: string
  editable: boolean
}) {
  const [editing, setEditing] = useState(false)
  const update = useUpdateEvent()
  const remove = useDeleteEvent()

  if (!editing) {
    return (
      <FeedCard
        event={event}
        timeZone={timeZone}
        onEdit={editable ? () => setEditing(true) : undefined}
      />
    )
  }

  return (
    <EventEditor
      event={event}
      timeZone={timeZone}
      isSaving={update.isPending}
      error={update.error}
      onCancel={() => {
        update.reset()
        setEditing(false)
      }}
      onSave={(patch) => {
        // Nothing changed: close rather than send an empty PATCH that would still stamp
        // `edited_at` and permanently freeze the row against re-extraction.
        if (Object.keys(patch).length === 0) {
          setEditing(false)
          return
        }
        update.mutate({ id: event.id, patch }, { onSuccess: () => setEditing(false) })
      }}
      onDelete={() => {
        setEditing(false)
        remove.mutate(event.id)
      }}
    />
  )
}
