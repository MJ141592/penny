/**
 * Correcting one event, in place.
 *
 * Full trust by design: anyone signed in edits anything, because the alternative — a permission
 * model — needs a users table this product deliberately does not have. What the form protects
 * instead is *accuracy*. Every field it offers is one an LLM can plausibly get wrong: the title
 * it wrote, the date it inferred, how sure it was about that date, and the one status field per
 * kind that changes what the card means ("scheduled" → "attended").
 *
 * It sends only the keys that actually changed. `details` merges server-side, so a one-field
 * correction to an appointment's status cannot blank its outcome or its follow-up actions.
 *
 * The date input is wall-clock in the HOUSEHOLD's timezone, never the browser's — a daughter in
 * Dubai fixing a typo must not silently move an appointment four hours.
 */

import { useState } from 'react'

import type { EventPatch } from '../api/endpoints'
import { fromLocalInputValue, toLocalInputValue } from '../lib/datetime'
import {
  assertNever,
  type Event,
  type OccurredAtPrecision,
  type SymptomEvent,
  type AppointmentEvent,
  type MedicationEvent,
  type NoteEvent,
} from '../types/api'
import { cx } from './cx'
import styles from './event-editor.module.css'

export interface EventEditorProps {
  event: Event
  timeZone: string
  onSave: (patch: EventPatch) => void
  onCancel: () => void
  onDelete: () => void
  isSaving?: boolean
  error?: unknown
}

const PRECISIONS: { value: OccurredAtPrecision; label: string }[] = [
  { value: 'exact', label: 'Exact time' },
  { value: 'day', label: 'That day' },
  { value: 'week', label: 'That week' },
  { value: 'month', label: 'That month' },
  { value: 'unknown', label: 'Date not known' },
]

export function EventEditor({
  event,
  timeZone,
  onSave,
  onCancel,
  onDelete,
  isSaving,
  error,
}: EventEditorProps) {
  const [title, setTitle] = useState(event.title)
  const [body, setBody] = useState(event.body ?? '')
  const [when, setWhen] = useState(() => toLocalInputValue(event.occurred_at, timeZone))
  const [precision, setPrecision] = useState<OccurredAtPrecision>(event.occurred_at_precision)
  const [detail, setDetail] = useState(() => detailChoice(event).value)
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  const choice = detailChoice(event)
  const occurredAt = fromLocalInputValue(when, timeZone)

  function submit() {
    const patch: EventPatch = {}
    if (title.trim() && title !== event.title) patch.title = title.trim()
    // An emptied box means "there is no summary", which is `null` — not the empty string.
    if (body !== (event.body ?? '')) patch.body = body.trim() === '' ? null : body
    if (occurredAt && occurredAt !== event.occurred_at) patch.occurred_at = occurredAt
    if (precision !== event.occurred_at_precision) patch.occurred_at_precision = precision
    if (detail !== choice.value) patch.details = { [choice.field]: detail }
    onSave(patch)
  }

  return (
    <form
      className={styles.editor}
      onSubmit={(submitted) => {
        submitted.preventDefault()
        submit()
      }}
    >
      <div className={styles.field}>
        <label className={styles.label} htmlFor={`title-${event.id}`}>
          Title
        </label>
        <input
          id={`title-${event.id}`}
          className={styles.input}
          value={title}
          maxLength={80}
          onChange={(change) => setTitle(change.target.value)}
        />
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`body-${event.id}`}>
          What happened
        </label>
        <textarea
          id={`body-${event.id}`}
          className={cx(styles.input, styles.textarea)}
          value={body}
          maxLength={400}
          rows={3}
          onChange={(change) => setBody(change.target.value)}
        />
      </div>

      <div className={styles.row}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`when-${event.id}`}>
            When
          </label>
          <input
            id={`when-${event.id}`}
            className={styles.input}
            type="datetime-local"
            value={when}
            onChange={(change) => setWhen(change.target.value)}
          />
          <span className={styles.hint}>Local time in {timeZone}.</span>
        </div>

        <div className={styles.field}>
          <label className={styles.label} htmlFor={`precision-${event.id}`}>
            How sure
          </label>
          <select
            id={`precision-${event.id}`}
            className={styles.input}
            value={precision}
            onChange={(change) => setPrecision(change.target.value as OccurredAtPrecision)}
          >
            {PRECISIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <span className={styles.hint}>Only as much of the date as this is shown.</span>
        </div>
      </div>

      <div className={styles.field}>
        <label className={styles.label} htmlFor={`detail-${event.id}`}>
          {choice.label}
        </label>
        <select
          id={`detail-${event.id}`}
          className={styles.input}
          value={detail}
          onChange={(change) => setDetail(change.target.value)}
        >
          {choice.options.map((option) => (
            <option key={option} value={option}>
              {sentenceCase(option)}
            </option>
          ))}
        </select>
      </div>

      {error instanceof Error ? (
        <p className={styles.error} role="alert">
          {error.message}
        </p>
      ) : null}

      <div className={styles.actions}>
        <button type="submit" className={cx(styles.button, styles.primary)} disabled={isSaving}>
          {isSaving ? 'Saving…' : 'Save'}
        </button>
        <button type="button" className={styles.button} onClick={onCancel} disabled={isSaving}>
          Cancel
        </button>

        <span className={styles.spacer} />

        {confirmingDelete ? (
          <>
            <span className={styles.confirm}>Delete this entry?</span>
            <button type="button" className={cx(styles.button, styles.danger)} onClick={onDelete}>
              Yes, delete
            </button>
            <button
              type="button"
              className={styles.button}
              onClick={() => setConfirmingDelete(false)}
            >
              Keep it
            </button>
          </>
        ) : (
          <button
            type="button"
            className={cx(styles.button, styles.quietDanger)}
            onClick={() => setConfirmingDelete(true)}
          >
            Delete
          </button>
        )}
      </div>
    </form>
  )
}

/**
 * The single `details` field worth offering per kind, chosen because it is the one that changes
 * what the card *means* rather than what it says. A fifth kind is a compile error here, same as
 * everywhere else the union is switched on.
 */
interface DetailChoice {
  field: string
  label: string
  value: string
  options: string[]
}

const SEVERITIES = ['unknown', 'mild', 'moderate', 'severe']
const STATUSES = ['scheduled', 'attended', 'cancelled', 'missed']
const ACTIONS = ['started', 'stopped', 'changed', 'missed', 'refilled', 'side_effect', 'other']
const CATEGORIES = ['logistics', 'mood', 'finance', 'equipment', 'admin', 'other']

function detailChoice(event: Event): DetailChoice {
  switch (event.kind) {
    case 'symptom':
      return choiceFor(event, 'severity', 'How bad', SEVERITIES)
    case 'appointment':
      return choiceFor(event, 'status', 'Status', STATUSES)
    case 'medication':
      return choiceFor(event, 'action', 'What happened', ACTIONS)
    case 'note':
      return choiceFor(event, 'category', 'About', CATEGORIES)
    default:
      return assertNever(event)
  }
}

function choiceFor(
  event: SymptomEvent | AppointmentEvent | MedicationEvent | NoteEvent,
  field: 'severity' | 'status' | 'action' | 'category',
  label: string,
  options: string[],
): DetailChoice {
  // An interface has no index signature, so the read is spelled out rather than assigned across.
  const details = event.details as unknown as Record<string, unknown>
  return { field, label, value: String(details[field] ?? options[0]), options }
}

function sentenceCase(value: string): string {
  const spaced = value.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}
