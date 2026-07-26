/**
 * The home screen, laid out as the care record from the Person Overview design: the identity
 * header and stat band on top, then the care timeline on the left with the context rail
 * (upcoming, medications, reminders, conditions) pinned on the right.
 *
 * Both lists come from the live API — `/api/upcoming` for the rail and a paginated `/api/feed`
 * for the timeline — and both render in the HOUSEHOLD's timezone, taken from the session rather
 * than from the browser, so two siblings in two countries see the same day dividers over the
 * same events.
 *
 * `counts.messages` is passed down because it is the only thing that can tell the two empty
 * feeds apart: a family who has imported nothing needs to be sent to the import flow, and a
 * family whose import found nothing must not be told to do it again.
 *
 * NO REPORTS QUERY HERE, deliberately. Reports are deferred, so `/api/reports` does not exist:
 * calling it made the home screen fire a guaranteed 404 on every single mount, for a section
 * that could never have anything in it. `useReports` and the "Reports" block below it come back
 * together, in one diff, when the route ships. The "Generate clinical summary" affordance above
 * the timeline is that feature's front door and does nothing yet for the same reason.
 */

import { Link } from 'react-router'

import { useFeed, useMembers, useSession, useUpcoming } from '../api/queries'
import { CareRail, EventFeed, RecordHeader, UpcomingPanel } from '../components'
import styles from './routes.module.css'

/** Only used before the session lands; every real render uses the household's own zone. */
const FALLBACK_TIMEZONE = 'Europe/London'

export function FeedRoute() {
  const session = useSession()
  const feed = useFeed()
  const upcoming = useUpcoming()
  const members = useMembers()

  const household = session.data?.household
  const timeZone = household?.timezone ?? FALLBACK_TIMEZONE
  const events = feed.data?.pages.flatMap((page) => page.events) ?? []

  return (
    <>
      <RecordHeader household={household} memberCount={members.data?.length} />

      <div className={styles.columns}>
        <div className={styles.feedColumn}>
          <div className={styles.timelineHead}>
            <div>
              <h1 className={styles.timelineTitle}>Care timeline</h1>
              <p className={styles.hint}>
                Everything the family has recorded, newest first. Times shown in {timeZone}.
              </p>
            </div>
            <button type="button" className={styles.summaryLink}>
              Generate clinical summary ↧
            </button>
          </div>

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
        </div>

        <CareRail>
          <UpcomingPanel
            events={upcoming.data?.events}
            timeZone={timeZone}
            isPending={upcoming.isPending}
            error={upcoming.error}
          />
        </CareRail>
      </div>
    </>
  )
}
