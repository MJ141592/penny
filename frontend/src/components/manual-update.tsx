import { useEffect, useState, type FormEvent } from 'react'

import { useCreateEvent } from '../api/queries'
import type { EventKind } from '../types/api'
import styles from './manual-update.module.css'

const KINDS: { value: EventKind; label: string }[] = [
  { value: 'note', label: 'Care note' },
  { value: 'symptom', label: 'Symptom' },
  { value: 'appointment', label: 'Appointment' },
  { value: 'medication', label: 'Medication' },
]

function localDateTime(): string {
  const now = new Date()
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset())
  return now.toISOString().slice(0, 16)
}

function detailsFor(kind: EventKind, title: string): Record<string, unknown> {
  if (kind === 'symptom') return { symptom: title, severity: 'unknown' }
  if (kind === 'appointment') return { appointment_kind: 'other', status: 'attended' }
  if (kind === 'medication') return { medication_name: title, action: 'other' }
  return { category: 'other' }
}

export function ManualUpdate({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const create = useCreateEvent()
  const [kind, setKind] = useState<EventKind>('note')
  const [title, setTitle] = useState('')
  const [body, setBody] = useState('')
  const [occurredAt, setOccurredAt] = useState(localDateTime)

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !create.isPending) onClose()
    }
    globalThis.addEventListener('keydown', onKeyDown)
    return () => globalThis.removeEventListener('keydown', onKeyDown)
  }, [create.isPending, onClose, open])

  if (!open) return null

  const submit = (event: FormEvent) => {
    event.preventDefault()
    create.mutate(
      {
        kind,
        title: title.trim(),
        body: body.trim() || null,
        occurred_at: new Date(occurredAt).toISOString(),
        occurred_at_precision: 'exact',
        details: detailsFor(kind, title.trim()),
      },
      {
        onSuccess: () => {
          setKind('note')
          setTitle('')
          setBody('')
          setOccurredAt(localDateTime())
          onClose()
        },
      },
    )
  }

  return (
    <div className={styles.backdrop} role="presentation" onMouseDown={onClose}>
      <section
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="manual-update-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className={styles.heading}>
          <div>
            <p className={styles.eyebrow}>Care timeline</p>
            <h2 id="manual-update-title">Add an update</h2>
          </div>
          <button type="button" className={styles.close} aria-label="Close" onClick={onClose}>
            ×
          </button>
        </div>

        <form className={styles.form} onSubmit={submit}>
          <fieldset className={styles.kinds}>
            <legend>Type</legend>
            {KINDS.map((option) => (
              <label key={option.value} className={styles.kind}>
                <input
                  type="radio"
                  name="kind"
                  value={option.value}
                  checked={kind === option.value}
                  onChange={() => setKind(option.value)}
                />
                {option.label}
              </label>
            ))}
          </fieldset>

          <label className={styles.field}>
            <span>What happened?</span>
            <input
              autoFocus
              required
              maxLength={80}
              value={title}
              onChange={(event) => setTitle(event.target.value)}
            />
          </label>

          <label className={styles.field}>
            <span>Details</span>
            <textarea
              rows={4}
              maxLength={400}
              value={body}
              onChange={(event) => setBody(event.target.value)}
              placeholder="What you saw, what was said, and what happens next."
            />
          </label>

          <label className={styles.field}>
            <span>When</span>
            <input
              required
              type="datetime-local"
              value={occurredAt}
              onChange={(event) => setOccurredAt(event.target.value)}
            />
          </label>

          {create.error ? <p className={styles.error}>{create.error.message}</p> : null}

          <div className={styles.actions}>
            <button type="button" className={styles.cancel} onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className={styles.submit} disabled={!title.trim() || create.isPending}>
              {create.isPending ? 'Adding…' : 'Add to record'}
            </button>
          </div>
        </form>
      </section>
    </div>
  )
}
