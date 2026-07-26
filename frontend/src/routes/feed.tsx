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

import { useState } from 'react'
import { Link, useNavigate } from 'react-router'

import { useCreateReport, useFeed, useMembers, useSession, useUpcoming } from '../api/queries'
import { CareRail, EventFeed, ManualUpdate, RecordHeader, UpcomingPanel } from '../components'
import styles from './routes.module.css'

/** Only used before the session lands; every real render uses the household's own zone. */
const FALLBACK_TIMEZONE = 'Europe/London'

export function FeedRoute() {
  const navigate = useNavigate()
  const [addingUpdate, setAddingUpdate] = useState(false)
  const session = useSession()
  const feed = useFeed()
  const upcoming = useUpcoming()
  const members = useMembers()
  const createReport = useCreateReport()

  const household = session.data?.household
  const timeZone = household?.timezone ?? FALLBACK_TIMEZONE
  const events = feed.data?.pages.flatMap((page) => page.events) ?? []

  return (
    <>
      <RecordHeader
        household={household}
        memberCount={members.data?.length}
        counts={session.data?.counts}
        onAddUpdate={() => setAddingUpdate(true)}
      />

      <div className={styles.columns}>
        <div className={styles.feedColumn}>
          <div className={styles.timelineHead}>
            <div>
              <h1 className={styles.timelineTitle}>Care timeline</h1>
              <p className={styles.hint}>
                Everything the family has recorded, newest first. Times shown in {timeZone}.
              </p>
            </div>
            <button
              type="button"
              className={styles.summaryLink}
              disabled={createReport.isPending}
              onClick={() =>
                createReport.mutate(30, {
                  onSuccess: (report) => navigate(`/reports/${report.id}`),
                })
              }
            >
              {createReport.isPending ? 'Generating…' : 'Generate clinical summary ↧'}
            </button>
          </div>

          {createReport.error ? <p className={styles.error}>{createReport.error.message}</p> : null}

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

          {feed.isSuccess && !feed.hasNextPage && events.length > 0 ? (
            <p className={styles.hint} style={{ marginTop: 20, textAlign: 'center' }}>
              That is the whole history Penny has.{' '}
              <Link to="/import">Import an older export</Link> to go further back.
            </p>
          ) : null}
        </div>

        <CareRail events={events}>
          <UpcomingPanel
            events={upcoming.data?.events}
            timeZone={timeZone}
            isPending={upcoming.isPending}
            error={upcoming.error}
          />
        </CareRail>
      </div>

      <ManualUpdate open={addingUpdate} onClose={() => setAddingUpdate(false)} />
    </>
  )
}
