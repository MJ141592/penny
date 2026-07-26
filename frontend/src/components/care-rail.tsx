/**
 * The right-hand context panel from the Person Overview design: what's coming up (live data,
 * passed in as children), then medications, Penny's reminder loop, and long-term conditions.
 *
 * Everything below the children is DESIGN PLACEHOLDER content — medications, reminders and
 * conditions have no API yet. The sections are laid out exactly as the design wants them so
 * that wiring them up later is a data change, not a layout change.
 */

import styles from './care-rail.module.css'

const MEDICATIONS = [
  { name: 'Ramipril 2.5 mg', detail: 'Dose lowered Mon 20 Jul', changed: true },
  { name: 'Furosemide 40 mg', detail: 'Mornings · since Nov 2023', changed: false },
  { name: 'Atorvastatin 20 mg', detail: 'Nightly · since 2017', changed: false },
]

const REMINDERS = [
  {
    time: '08:00',
    doses: 'Ramipril 2.5 mg · Furosemide 40 mg',
    status: '✓ taken',
    done: true,
    note: 'Penny asked Yuval on WhatsApp — "Has she taken her morning tablets?" · replied 08:04',
  },
  {
    time: '20:00',
    doses: 'Atorvastatin 20 mg',
    status: 'tonight',
    done: false,
    note: 'Penny will ask Liz on WhatsApp at 20:00',
  },
]

const CONDITIONS = [
  { name: 'Heart failure', detail: 'Nov 2023 · yearly echo · daily fluid watch' },
  { name: 'Hypertension', detail: 'Since 2014 · reviewed Jul 2026' },
  { name: 'Osteoarthritis, knees', detail: 'Since 2019 · managed with physio' },
]

function RailLabel({ children, action }: { children: React.ReactNode; action?: string }) {
  return (
    <div className={styles.labelRow}>
      <h3 className={styles.label}>{children}</h3>
      {action ? (
        <button type="button" className={styles.labelAction}>
          {action}
        </button>
      ) : null}
    </div>
  )
}

export function CareRail({ children }: { children?: React.ReactNode }) {
  return (
    <aside className={styles.rail}>
      {children}

      <section>
        <RailLabel action="History">Medications · 3 active</RailLabel>
        <div className={styles.meds}>
          {MEDICATIONS.map((med) => (
            <div key={med.name} className={styles.medRow}>
              <span className={med.changed ? styles.medBarChanged : styles.medBar} />
              <span className={styles.medText}>
                <span className={med.changed ? styles.medNameChanged : styles.medName}>
                  {med.name}
                </span>
                <span className={styles.medDetail}>{med.detail}</span>
              </span>
              {med.changed ? <span className={styles.medStatus}>Changed</span> : null}
            </div>
          ))}
        </div>
      </section>

      <section>
        <RailLabel action="+ New">Reminders</RailLabel>
        <div className={styles.reminderCard}>
          <p className={styles.reminderIntro}>
            Penny WhatsApps the person on duty at each dose time and files their reply back into
            the record.
          </p>
          {REMINDERS.map((reminder) => (
            <div key={reminder.time} className={styles.reminder}>
              <div className={styles.reminderRow}>
                <span className={styles.reminderTime}>{reminder.time}</span>
                <span className={styles.reminderDoses}>{reminder.doses}</span>
                <span className={reminder.done ? styles.reminderDone : styles.reminderPending}>
                  {reminder.status}
                </span>
              </div>
              <p className={styles.reminderNote}>{reminder.note}</p>
            </div>
          ))}
          <p className={styles.reminderFooter}>No reply within 30 min → Penny asks Owen instead</p>
        </div>
      </section>

      <section>
        <RailLabel>Living with</RailLabel>
        <div className={styles.conditions}>
          {CONDITIONS.map((condition) => (
            <div key={condition.name}>
              <div className={styles.conditionName}>{condition.name}</div>
              <div className={styles.conditionDetail}>{condition.detail}</div>
            </div>
          ))}
        </div>
      </section>
    </aside>
  )
}
