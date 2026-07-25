import { Link, useParams } from 'react-router'

import { useHousehold, useReport } from '../api/queries'
import { formatDate } from '../lib/datetime'
import styles from './routes.module.css'

export function ReportRoute() {
  const { id = '' } = useParams()
  const household = useHousehold()
  const report = useReport(id)
  const timeZone = household?.timezone ?? 'Europe/London'

  if (report.isPending) return <p className={styles.hint}>Loading the report…</p>
  if (report.error) {
    return (
      <div className={styles.empty}>
        <h2>Report unavailable</h2>
        <p>{report.error.message}</p>
        <p>
          <Link to="/">Back to the feed</Link>
        </p>
      </div>
    )
  }

  const data = report.data
  return (
    <>
      <div className={styles.pageHeader}>
        <h1>{data.title}</h1>
        <p className={styles.hint}>
          {formatDate(data.period_start, timeZone)} to {formatDate(data.period_end, timeZone)}
          {data.generated_at ? ` · generated ${formatDate(data.generated_at, timeZone)}` : null}
        </p>
      </div>

      {data.status !== 'complete' ? (
        <div className={styles.empty}>
          <h2>Still being written</h2>
          <p>{data.error ?? 'Come back in a minute.'}</p>
        </div>
      ) : null}

      {data.urgent_flag ? <p className={styles.error}>{data.urgent_reason}</p> : null}
      {data.summary ? <p className={styles.body}>{data.summary}</p> : null}

      {data.sections.map((section) => (
        <section key={section.heading} className={styles.card}>
          <h2>{section.heading}</h2>
          <p>{section.body_markdown}</p>
          {/* Citations are resolved server-side; a handle that did not resolve never gets here. */}
          {section.citations.map((citation) => (
            <p key={citation.handle} className={styles.citation}>
              [{citation.handle}] {formatDate(citation.occurred_at, timeZone)} · {citation.title}
            </p>
          ))}
        </section>
      ))}

      <ReportList heading="Questions for the doctor" items={data.questions_for_the_doctor} />
      <ReportList heading="Worth watching" items={data.watch_items} />
      <ReportList heading="Not recorded" items={data.data_gaps} />
    </>
  )
}

function ReportList({ heading, items }: { heading: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <section className={styles.section}>
      <h2>{heading}</h2>
      <ul>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </section>
  )
}
