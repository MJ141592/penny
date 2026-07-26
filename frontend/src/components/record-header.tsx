/**
 * The identity block at the top of the care record: who this record is about, who keeps it,
 * and the handful of facts a family reaches for first (from the Person Overview design).
 *
 * The name and contributor count are live data. The stat band is DESIGN PLACEHOLDER content:
 * none of NHS number / allergies / ReSPECT / baseline exist in the API yet, so they are
 * hard-coded here until the backend grows a person profile. When it does, this component
 * takes them as props and the constants go.
 */

import type { Household } from '../types/api'
import styles from './record-header.module.css'

const PLACEHOLDER_STATS = [
  { label: 'NHS number', value: '485 777 3456' },
  { label: 'Allergies', value: 'Penicillin · adhesive plasters', alert: true },
  { label: 'ReSPECT form', value: 'On file · updated Jan 2026 · View' },
  { label: 'Her baseline', value: 'Mobile with frame · breathless on one flight of stairs' },
]

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
}: {
  household: Household | undefined
  memberCount: number | undefined
}) {
  const name = household?.care_recipient_name ?? '…'

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
        <button type="button" className={styles.addUpdate}>
          + Add an update
        </button>
      </div>

      <dl className={styles.statBand}>
        {PLACEHOLDER_STATS.map((stat) => (
          <div key={stat.label} className={styles.stat}>
            <dt className={stat.alert ? styles.statLabelAlert : styles.statLabel}>{stat.label}</dt>
            <dd className={styles.statValue}>{stat.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
