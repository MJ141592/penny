import type { Household, Session } from '../types/api'
import styles from './record-header.module.css'

function initials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('')
}

export function RecordHeader({
  household,
  memberCount,
  counts,
  onAddUpdate,
}: {
  household: Household | undefined
  memberCount: number | undefined
  counts: Session['counts'] | undefined
  onAddUpdate: () => void
}) {
  const name = household?.care_recipient_name || 'Care record'
  const stats = [
    { label: 'Family contributors', value: String(memberCount ?? 0) },
    { label: 'Timeline entries', value: String(counts?.events ?? 0) },
    { label: 'Source messages', value: (counts?.messages ?? 0).toLocaleString() },
    { label: 'Household timezone', value: household?.timezone ?? 'Europe/London' },
  ]

  return (
    <div className={styles.header}>
      <div className={styles.identity}>
        <span className={styles.avatar} aria-hidden="true">
          {initials(name)}
        </span>
        <div>
          <h1 className={styles.name}>{name}</h1>
          <p className={styles.contributors}>
            {memberCount
              ? `${memberCount} people contribute to this record`
              : 'A record kept by the whole family'}
          </p>
        </div>
        <button type="button" className={styles.addUpdate} onClick={onAddUpdate}>
          + Add an update
        </button>
      </div>

      <dl className={styles.statBand}>
        {stats.map((stat) => (
          <div key={stat.label} className={styles.stat}>
            <dt className={styles.statLabel}>{stat.label}</dt>
            <dd className={styles.statValue}>{stat.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
