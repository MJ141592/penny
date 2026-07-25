/**
 * Turns an `Event` into the label + fact list a card renders.
 *
 * This is the switch the discriminated union exists for: `assertNever` in the default arm means
 * adding a fifth event kind to the API fails `tsc -b` here, instead of shipping a card that
 * renders a title and a blank body because nobody remembered this file.
 */

import { assertNever, type Event, type EventKind } from '../types/api'

export interface EventFact {
  label: string
  value: string
}

const KIND_LABELS: Record<EventKind, string> = {
  symptom: 'Symptom',
  appointment: 'Appointment',
  medication: 'Medication',
  note: 'Note',
}

export function eventKindLabel(kind: EventKind): string {
  return KIND_LABELS[kind]
}

const APPOINTMENT_KIND_LABELS: Record<string, string> = {
  gp: 'GP',
  specialist: 'Specialist',
  hospital: 'Hospital',
  test: 'Test',
  therapy: 'Therapy',
  other: 'Other',
}

const MEDICATION_ACTION_LABELS: Record<string, string> = {
  started: 'Started',
  stopped: 'Stopped',
  changed: 'Dose changed',
  missed: 'Dose missed',
  refilled: 'Prescription collected',
  side_effect: 'Possible side effect',
  other: 'Other',
}

function fact(label: string, value: string | null | undefined): EventFact | null {
  return value ? { label, value } : null
}

function facts(...candidates: (EventFact | null)[]): EventFact[] {
  return candidates.filter((candidate): candidate is EventFact => candidate !== null)
}

/** The kind-specific detail lines, in the order they should be read. */
export function eventFacts(event: Event): EventFact[] {
  switch (event.kind) {
    case 'symptom':
      return facts(
        fact('Symptom', event.details.symptom),
        event.details.severity === 'unknown' ? null : fact('Severity', event.details.severity),
        fact('Where', event.details.body_site),
        fact('Duration', event.details.duration_text),
      )
    case 'appointment':
      return facts(
        fact('Type', APPOINTMENT_KIND_LABELS[event.details.appointment_kind]),
        fact('Status', event.details.status),
        fact('With', event.details.provider_name),
        fact('Where', event.details.location),
        fact('Who went', event.details.attendees.join(', ')),
        fact('Outcome', event.details.outcome),
        fact('Follow-up', event.details.follow_up_actions.join('; ')),
      )
    case 'medication':
      return facts(
        fact('Medication', event.details.medication_name),
        fact('What happened', MEDICATION_ACTION_LABELS[event.details.action]),
        fact('Dose', event.details.dose_text),
        fact('Prescribed by', event.details.prescriber),
      )
    case 'note':
      return facts(fact('Category', event.details.category))
    default:
      return assertNever(event)
  }
}
