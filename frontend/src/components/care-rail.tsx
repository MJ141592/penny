import type { Event, MedicationEvent, SymptomEvent } from '../types/api'
import styles from './care-rail.module.css'

function latestMedications(events: Event[]): MedicationEvent[] {
  const seen = new Set<string>()
  const active: MedicationEvent[] = []
  for (const event of events) {
    if (event.kind !== 'medication') continue
    const key = event.details.medication_name.trim().toLocaleLowerCase()
    if (!key || seen.has(key)) continue
    seen.add(key)
    if (event.details.action !== 'stopped') active.push(event)
  }
  return active.slice(0, 5)
}

function recentSymptoms(events: Event[]): SymptomEvent[] {
  const seen = new Set<string>()
  return events
    .filter((event): event is SymptomEvent => event.kind === 'symptom')
    .filter((event) => {
      const key = (event.details.symptom || event.title).trim().toLocaleLowerCase()
      if (!key || seen.has(key)) return false
      seen.add(key)
      return true
    })
    .slice(0, 5)
}

export function CareRail({
  events,
  children,
}: {
  events: Event[]
  children?: React.ReactNode
}) {
  const medications = latestMedications(events)
  const symptoms = recentSymptoms(events)

  return (
    <aside className={styles.rail}>
      {children}

      <section>
        <h3 className={styles.label}>Medication in the record</h3>
        {medications.length ? (
          <div className={styles.meds}>
            {medications.map((medication) => (
              <div key={medication.id} className={styles.medRow}>
                <span
                  className={
                    medication.details.action === 'changed' ? styles.medBarChanged : styles.medBar
                  }
                />
                <span className={styles.medText}>
                  <span className={styles.medName}>{medication.details.medication_name}</span>
                  <span className={styles.medDetail}>
                    {[medication.details.dose_text, medication.details.action]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className={styles.empty}>No medication updates recorded.</p>
        )}
      </section>

      <section>
        <h3 className={styles.label}>Recent symptoms</h3>
        {symptoms.length ? (
          <div className={styles.conditions}>
            {symptoms.map((symptom) => (
              <div key={symptom.id}>
                <div className={styles.conditionName}>
                  {symptom.details.symptom || symptom.title}
                </div>
                <div className={styles.conditionDetail}>
                  {symptom.details.severity === 'unknown'
                    ? 'Severity not recorded'
                    : `${symptom.details.severity} severity`}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className={styles.empty}>No symptoms recorded.</p>
        )}
      </section>
    </aside>
  )
}
