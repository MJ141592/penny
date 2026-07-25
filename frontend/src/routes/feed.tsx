/**
 * The home screen: what is coming up, then everything that has happened, newest first.
 *
 * Both lists come from the live API now — `/api/upcoming` for the panel and a paginated
 * `/api/feed` under it — and both render in the HOUSEHOLD's timezone, taken from the session
 * rather than from the browser, so two siblings in two countries see the same day dividers over
 * the same events.
 *
 * `counts.messages` is passed down because it is the only thing that can tell the two empty feeds
 * apart: a family who has imported nothing needs to be sent to the import flow, and a family
 * whose import found nothing must not be told to do it again.
 *
 * NO REPORTS QUERY HERE, deliberately. Reports are deferred, so `/api/reports` does not exist:
 * calling it made the home screen fire a guaranteed 404 on every single mount, for a section
 * that could never have anything in it. `useReports` and the "Reports" block below it come back
 * together, in one diff, when the route ships. `queries.useReports` is left in place for that.
 */

import { Link } from 'react-router'

import { useFeed, useSession, useUpcoming } from '../api/queries'
import { EventFeed, UpcomingPanel } from '../components'
import styles from './routes.module.css'

/** Only used before the session lands; every real render uses the household's own zone. */
const FALLBACK_TIMEZONE = 'Europe/London'

export function FeedRoute() {
  const session = useSession()
  const feed = useFeed()
  const upcoming = useUpcoming()

  const household = session.data?.household
  const timeZone = household?.timezone ?? FALLBACK_TIMEZONE
  const events = feed.data?.pages.flatMap((page) => page.events) ?? []

  return (
    <>
      <div className={styles.pageHeader}>
        <h1>{household ? `${household.care_recipient_name}'s history` : 'History'}</h1>
        <p className={styles.hint}>
          Everything the family has recorded, newest first. Times shown in {timeZone}.
        </p>
      </div>

      <UpcomingPanel
        events={upcoming.data?.events}
        timeZone={timeZone}
        isPending={upcoming.isPending}
        error={upcoming.error}
      />

      <EventFeed
        events={events}
        timeZone={timeZone}
        isPending={feed.isPending}
        error={feed.error}
        onRetry={() => void feed.refetch()}
        messageCount={session.data?.counts.messages}
        editable
      />

      {feed.hasNextPage ? (
        <div className={styles.actions} style={{ marginTop: 20 }}>
          <button
            type="button"
            className={styles.button}
            disabled={feed.isFetchingNextPage}
            onClick={() => void feed.fetchNextPage()}
          >
            {feed.isFetchingNextPage ? 'Loading…' : 'Load older entries'}
          </button>
        </div>
      ) : null}

      {/* `next_before: null` is the server saying there is nothing older — say so, don't just stop. */}
      {feed.isSuccess && !feed.hasNextPage && events.length > 0 ? (
        <p className={styles.hint} style={{ marginTop: 20, textAlign: 'center' }}>
          That is the whole history Penny has.{' '}
          <Link to="/import">Import an older export</Link> to go further back.
        </p>
      ) : null}
    </>
  )
}
