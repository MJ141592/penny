/**
 * Picking an IANA timezone, without letting anyone type one.
 *
 * A free-text box here is a trap: `Europe/Londn` is a 422 from the server, and worse, a plausible
 * wrong answer like `Europe/Dublin` is accepted silently and shifts nothing — until a DST edge
 * moves a day divider. The browser already knows every zone it supports, so the list comes from
 * `Intl.supportedValuesOf` and the only reachable values are real ones.
 *
 * The current value is always inserted into the options even if this browser has never heard of
 * it, so an older engine cannot silently rewrite the household's zone to whatever sorts first.
 */

import { useMemo } from 'react'

import styles from './routes.module.css'

/** Enough to be usable on an engine without `supportedValuesOf`; the server is still the judge. */
const FALLBACK_ZONES = [
  'Europe/London',
  'Europe/Dublin',
  'Europe/Paris',
  'Europe/Berlin',
  'Europe/Madrid',
  'Europe/Lisbon',
  'Europe/Warsaw',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
  'America/Toronto',
  'Asia/Dubai',
  'Asia/Kolkata',
  'Asia/Singapore',
  'Asia/Tokyo',
  'Australia/Sydney',
  'Pacific/Auckland',
  'UTC',
]

function supportedZones(): string[] {
  const intl = Intl as typeof Intl & { supportedValuesOf?: (key: string) => string[] }
  try {
    const values = intl.supportedValuesOf?.('timeZone')
    if (values && values.length > 0) return values
  } catch {
    /* some engines throw on an unknown key rather than returning undefined */
  }
  return FALLBACK_ZONES
}

export interface TimezoneFieldProps {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  hint?: string
}

export function TimezoneField({ id, label, value, onChange, hint }: TimezoneFieldProps) {
  const zones = useMemo(() => {
    const all = supportedZones()
    return all.includes(value) ? all : [value, ...all]
  }, [value])

  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className={styles.input}
        value={value}
        onChange={(change) => onChange(change.target.value)}
      >
        {zones.map((zone) => (
          <option key={zone} value={zone}>
            {zone.replace(/_/g, ' ')}
          </option>
        ))}
      </select>
      {hint ? <span className={styles.hint}>{hint}</span> : null}
    </div>
  )
}
