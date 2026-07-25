/**
 * What is still to come — the same `Event` objects the history feed renders, filtered by
 * `occurred_at > now()` on the server and shown soonest-first. "Upcoming" is a query, not an
 * entity: there is no separate model, no separate card, and nothing here that the feed doesn't
 * already know.
 *
 * The countdown is counted in the household's timezone, by comparing day keys rather than
 * subtracting instants: an appointment at 09:00 tomorrow is "Tomorrow" even when it is 21 hours
 * away, and 08:00 today is "Today" even when it has just passed 07:00.
 */

import { dayKey, formatOccurredAt } from '../lib/datetime'
import { eventKindLabel } from '../lib/events'
import type { Event, EventKind } from '../types/api'
import { cx } from './cx'
import { UpcomingEmptyState } from './empty-state'
import { ErrorState } from './query-state'
import styles from './upcoming-panel.module.css'

const KIND_CLASS: Record<EventKind, string | undefined> = {
  symptom: styles.symptom,
  appointment: styles.appointment,
  medication: styles.medication,
  note: styles.note,
}

export interface UpcomingPanelProps {
  events: Event[] | undefined
  timeZone: string
  isPending?: boolean
  error?: unknown
}

export function UpcomingPanel({ events, timeZone, isPending, error }: UpcomingPanelProps) {
  return (
    <section className={styles.panel} aria-labelledby="upcoming-heading">
      <h2 id="upcoming-heading" className={styles.heading}>
        Coming up
      </h2>
      <PanelContents events={events} timeZone={timeZone} isPending={isPending} error={error} />
    </section>
  )
}

function PanelContents({ events, timeZone, isPending, error }: UpcomingPanelProps) {
  if (error) return <ErrorState error={error} title="Couldn't load what's coming up" />
  if (isPending || !events) return <p className={styles.when}>Loading…</p>
  // An empty list is the common case for a family with nothing booked, not a failure to load.
  if (events.length === 0) return <UpcomingEmptyState />

  return (
    <ol className={styles.list}>
      {events.map((event) => (
        // Order comes from the API (ascending); re-sorting here would only hide a backend bug.
        <li key={event.id} className={cx(styles.row, KIND_CLASS[event.kind])}>
          <span className={styles.countdown}>{countdown(event.occurred_at, timeZone)}</span>
          <span className={styles.title}>{event.title}</span>
          <span className={styles.when}>
            <span className={styles.kind}>{eventKindLabel(event.kind)}</span>
            <span className={styles.sep} aria-hidden="true">
              ·
            </span>
            <time dateTime={event.occurred_at}>
              {formatOccurredAt(event.occurred_at, event.occurred_at_precision, timeZone)}
            </time>
          </span>
        </li>
      ))}
    </ol>
  )
}

const DAY_MS = 86_400_000

function countdown(iso: string, timeZone: string, now = new Date()): string {
  const today = Date.parse(dayKey(now.toISOString(), timeZone))
  const then = Date.parse(dayKey(iso, timeZone))
  const days = Math.round((then - today) / DAY_MS)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Tomorrow'
  return `In ${days} days`
}
