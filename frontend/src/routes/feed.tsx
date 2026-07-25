import { Link } from 'react-router'

import { useFeed, useHousehold, useReports, useUpcoming } from '../api/queries'
import { formatDate, formatOccurredAt, groupByDay } from '../lib/datetime'
import { eventFacts, eventKindLabel } from '../lib/events'
import type { Event, EventKind } from '../types/api'
import styles from './routes.module.css'

const KIND_CLASS: Record<EventKind, string | undefined> = {
  symptom: styles.kindSymptom,
  appointment: styles.kindAppointment,
  medication: styles.kindMedication,
  note: styles.kindNote,
}

export function FeedRoute() {
  const household = useHousehold()
  const feed = useFeed()
  const upcoming = useUpcoming()
  const reports = useReports()

  const timeZone = household?.timezone ?? 'Europe/London'
  const events = feed.data?.pages.flatMap((page) => page.events) ?? []
  const days = groupByDay(events, timeZone, (event) => event.occurred_at)

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>{household ? `${household.care_recipient_name}'s history` : 'History'}</h1>
        <p className={styles.hint}>
          Everything the family has recorded, newest first. Times shown in {timeZone}.
        </p>
      </div>

      {/* UpcomingPanel slot — "upcoming" is a query (occurred_at &gt; now), not an entity. */}
      <section className={styles.panel}>
        <h2>Coming up</h2>
        {upcoming.isPending ? <p className={styles.hint}>Loading…</p> : null}
        {upcoming.data?.events.length === 0 ? (
          <p className={styles.hint}>Nothing scheduled. An empty list is the common case.</p>
        ) : null}
        {upcoming.data?.events.map((event) => (
          <div key={event.id} className={styles.panelRow}>
            <span className={styles.meta}>{formatDate(event.occurred_at, timeZone)}</span>
            <span className={styles.title}>{event.title}</span>
          </div>
        ))}
      </section>

      {reports.data && reports.data.length > 0 ? (
        <section className={styles.section}>
          <h2>Reports</h2>
          {reports.data.map((report) => (
            <div key={report.id} className={styles.panelRow}>
              <Link to={`/reports/${report.id}`}>{report.title}</Link>
              <span className={styles.meta}>{report.status}</span>
            </div>
          ))}
        </section>
      ) : null}

      {feed.error ? <p className={styles.error}>{feed.error.message}</p> : null}
      {feed.isPending ? <p className={styles.hint}>Loading the feed…</p> : null}

      {feed.isSuccess && events.length === 0 ? (
        <div className={styles.empty}>
          <h2>Nothing here yet</h2>
          <p>
            Penny has no events for this household. <Link to="/import">Import a chat export</Link>{' '}
            to fill in the history.
          </p>
        </div>
      ) : null}

      {days.map((day) => (
        <section key={day.key}>
          {/* DayDivider slot. */}
          <h2 className={styles.dayHeading}>{day.heading}</h2>
          {day.items.map((event) => (
            <EventCard key={event.id} event={event} timeZone={timeZone} />
          ))}
        </section>
      ))}

      {feed.hasNextPage ? (
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.button}
            disabled={feed.isFetchingNextPage}
            onClick={() => void feed.fetchNextPage()}
          >
            {feed.isFetchingNextPage ? 'Loading…' : 'Load older events'}
          </button>
        </div>
      ) : null}
    </>
  )
}

/**
 * Placeholder for the real `FeedCard`. It renders every field the contract defines so the
 * fixtures are actually exercised; the component track replaces it with the styled version.
 */
function EventCard({ event, timeZone }: { event: Event; timeZone: string }) {
  const facts = eventFacts(event)
  return (
    <article className={styles.card}>
      <div className={styles.cardTop}>
        <span className={`${styles.kind} ${KIND_CLASS[event.kind] ?? ''}`}>
          {eventKindLabel(event.kind)}
        </span>
        <span className={styles.title}>{event.title}</span>
        <span className={styles.meta}>
          {formatOccurredAt(event.occurred_at, event.occurred_at_precision, timeZone)}
        </span>
        {event.actor ? <span className={styles.meta}>· {event.actor.display_name}</span> : null}
        {event.edited_at ? <span className={styles.edited}>edited</span> : null}
      </div>

      {event.body ? <p className={styles.body}>{event.body}</p> : null}

      {facts.length > 0 ? (
        <dl className={styles.facts}>
          {facts.map((item) => (
            <div key={item.label} style={{ display: 'contents' }}>
              <dt className={styles.factLabel}>{item.label}</dt>
              <dd className={styles.factValue}>{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {/* SourceDisclosure slot — the verbatim quote carries nearly all the trust value. */}
      {event.source_excerpts.length > 0 ? (
        <details className={styles.excerpts}>
          <summary>
            {event.source_excerpts.length === 1
              ? '1 message'
              : `${event.source_excerpts.length} messages`}
          </summary>
          {event.source_excerpts.map((excerpt) => (
            <blockquote key={excerpt.message_id} className={styles.quote}>
              {excerpt.quote}
              <div className={styles.quoteMeta}>
                {excerpt.sender} · {formatDate(excerpt.sent_at, timeZone)}
              </div>
            </blockquote>
          ))}
        </details>
      ) : null}
    </article>
  )
}
