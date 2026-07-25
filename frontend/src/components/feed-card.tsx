/**
 * One care event, rendered.
 *
 * `treatment()` is a second exhaustive switch over `event.kind`, separate from `eventFacts()`: the
 * facts list is what an event *says*, this is how it *looks* — its hue, its glyph, and the one
 * signal worth reading without opening anything (how bad the symptom was, whether the appointment
 * actually happened). Both end in `assertNever`, so a fifth kind is a compile error here as well
 * as in `lib/events.ts` rather than a card that quietly renders as a grey note.
 */

import type { ReactNode } from 'react'

import { formatOccurredAt } from '../lib/datetime'
import { eventFacts, eventKindLabel } from '../lib/events'
import { assertNever, type AppointmentStatus, type Event, type Severity } from '../types/api'
import { cx } from './cx'
import styles from './feed-card.module.css'
import { SourceDisclosure } from './source-disclosure'

export interface FeedCardProps {
  event: Event
  timeZone: string
}

export function FeedCard({ event, timeZone }: FeedCardProps) {
  const look = treatment(event)
  // The headline signal is repeated verbatim in `eventFacts`; show it once, in the louder place.
  const facts = eventFacts(event).filter((item) => !look.omitFacts.includes(item.label))
  const approximate = event.occurred_at_precision !== 'exact' && event.occurred_at_precision !== 'day'

  return (
    <article className={cx(styles.card, look.className)}>
      <div className={styles.header}>
        <span className={styles.kind}>
          {look.glyph}
          {eventKindLabel(event.kind)}
        </span>
        {event.edited_at ? (
          <span className={styles.edited} title="A family member edited this event">
            edited
          </span>
        ) : null}
        <time
          className={cx(styles.when, approximate && styles.approx)}
          dateTime={event.occurred_at}
        >
          {formatOccurredAt(event.occurred_at, event.occurred_at_precision, timeZone)}
        </time>
      </div>

      <h3 className={styles.title}>{event.title}</h3>

      {event.actor ? (
        <p className={styles.byline}>
          Reported by <span className={styles.who}>{event.actor.display_name}</span>
        </p>
      ) : null}

      {look.badge ? <Badge {...look.badge} /> : null}

      {event.body ? <p className={styles.body}>{event.body}</p> : null}

      {facts.length > 0 ? (
        <dl className={styles.facts}>
          {facts.map((item) => (
            <div key={item.label} className={styles.factRow}>
              <dt className={styles.factLabel}>{item.label}</dt>
              <dd className={styles.factValue}>{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      <SourceDisclosure excerpts={event.source_excerpts} timeZone={timeZone} />
    </article>
  )
}

interface BadgeProps {
  /** Announced before the value, so the number of bars is never the only thing carrying it. */
  label: string
  value: string
  tone?: string | undefined
  meter?: number | undefined
}

function Badge({ label, value, tone, meter }: BadgeProps) {
  return (
    <p className={cx(styles.badge, tone)}>
      <span className={styles.badgeLabel}>{label}</span>
      {meter === undefined ? null : <Meter filled={meter} />}
      <span>{value}</span>
    </p>
  )
}

const METER_STEPS = [1, 2, 3]

function Meter({ filled }: { filled: number }) {
  return (
    <span className={styles.meter} aria-hidden="true">
      {METER_STEPS.map((step) => (
        <span key={step} className={cx(styles.meterStep, step <= filled && styles.meterStepOn)} />
      ))}
    </span>
  )
}

interface Treatment {
  className: string | undefined
  glyph: ReactNode
  badge: BadgeProps | null
  /** Fact labels the badge has taken over, so the card never says the same thing twice. */
  omitFacts: string[]
}

const SEVERITY_STEPS: Record<Severity, number> = { unknown: 0, mild: 1, moderate: 2, severe: 3 }

const STATUS_TONE: Record<AppointmentStatus, string | undefined> = {
  scheduled: undefined,
  attended: styles.toneOk,
  cancelled: undefined,
  missed: styles.toneDanger,
}

function treatment(event: Event): Treatment {
  switch (event.kind) {
    case 'symptom':
      return {
        className: styles.symptom,
        glyph: <PulseGlyph />,
        badge:
          event.details.severity === 'unknown'
            ? null
            : {
                label: 'Severity',
                value: capitalise(event.details.severity),
                meter: SEVERITY_STEPS[event.details.severity],
              },
        omitFacts: ['Severity'],
      }
    case 'appointment':
      return {
        className: styles.appointment,
        glyph: <CalendarGlyph />,
        badge: {
          label: 'Status',
          value: capitalise(event.details.status),
          tone: STATUS_TONE[event.details.status],
        },
        omitFacts: ['Status'],
      }
    case 'medication':
      return {
        className: styles.medication,
        glyph: <PillGlyph />,
        badge: {
          label: 'Medication',
          value: [event.details.medication_name, event.details.dose_text]
            .filter(Boolean)
            .join(' · '),
        },
        omitFacts: ['Medication', 'Dose'],
      }
    case 'note':
      return {
        className: styles.note,
        glyph: <NoteGlyph />,
        badge: { label: 'About', value: capitalise(event.details.category) },
        omitFacts: ['Category'],
      }
    default:
      return assertNever(event)
  }
}

function capitalise(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1)
}

/** Decorative: the kind's name sits next to every one of these in plain text. */
function glyphProps() {
  return {
    className: styles.glyph,
    width: 13,
    height: 13,
    viewBox: '0 0 16 16',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.7,
    'aria-hidden': true,
    focusable: 'false',
  } as const
}

function PulseGlyph() {
  return (
    <svg {...glyphProps()}>
      <path d="M1 8h3l2-5 3 10 2-5h4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function CalendarGlyph() {
  return (
    <svg {...glyphProps()}>
      <rect x="1.5" y="3" width="13" height="11.5" rx="1.5" />
      <path d="M1.5 6.5h13M5 1.5v3M11 1.5v3" strokeLinecap="round" />
    </svg>
  )
}

function PillGlyph() {
  return (
    <svg {...glyphProps()}>
      <rect x="1.5" y="4.5" width="13" height="7" rx="3.5" />
      <path d="M8 4.5v7" />
    </svg>
  )
}

function NoteGlyph() {
  return (
    <svg {...glyphProps()}>
      <path d="M3 1.5h6.5L13 5v9.5H3z" strokeLinejoin="round" />
      <path d="M9 1.5V5h4M5.5 8.5h5M5.5 11h3.5" strokeLinecap="round" />
    </svg>
  )
}
