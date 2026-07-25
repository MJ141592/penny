/**
 * Hand-written mirror of `docs/api-contract.md`.
 *
 * Hand-written, not generated, until M4: the backend does not exist yet, so there is no
 * `/openapi.json` to run `openapi-typescript` against. When it exists, this file is replaced by
 * the generated `api/types.ts` and any divergence is a bug in one of them.
 *
 * All timestamps are ISO 8601 UTC strings with a trailing `Z`. Money is a decimal string.
 */

export type EventKind = 'symptom' | 'appointment' | 'medication' | 'note'

/**
 * Drives rendering, not just storage: `exact` shows a time, `day` shows a date, `week`/`month`
 * show "week of…"/"in July". Passed through unchanged from the extraction model.
 */
export type OccurredAtPrecision = 'exact' | 'day' | 'week' | 'month' | 'unknown'

/** Verbatim evidence for an event. This is what makes the feed trustworthy. */
export interface SourceExcerpt {
  message_id: string
  sent_at: string
  sender: string
  quote: string
}

/** Who did or reported it. Comes from the WhatsApp sender, not from who logged in. */
export interface EventActor {
  member_id: string
  display_name: string
}

export interface EventBase {
  id: string
  /** Never null — an undated event falls back to its earliest source message's `sent_at`. */
  occurred_at: string
  occurred_at_precision: OccurredAtPrecision
  title: string
  body: string | null
  actor: EventActor | null
  /** May be empty for a human-authored event. */
  source_excerpts: SourceExcerpt[]
  /** Non-null means a human edited it and re-extraction will never overwrite it again. */
  edited_at: string | null
  created_at: string
}

export type Severity = 'mild' | 'moderate' | 'severe' | 'unknown'

export interface SymptomDetails {
  symptom: string
  severity: Severity
  body_site: string | null
  duration_text: string | null
}

export type AppointmentKind = 'gp' | 'specialist' | 'hospital' | 'test' | 'therapy' | 'other'
export type AppointmentStatus = 'scheduled' | 'attended' | 'cancelled' | 'missed'

/**
 * One appointment is ONE event for its whole life: created when the family mentions booking it,
 * then gaining `outcome`, `follow_up_actions` and `status: "attended"` when they discuss it
 * afterwards. There is no separate outcome event and no parent/child link.
 */
export interface AppointmentDetails {
  appointment_kind: AppointmentKind
  provider_name: string | null
  location: string | null
  attendees: string[]
  outcome: string | null
  follow_up_actions: string[]
  status: AppointmentStatus
}

export type MedicationAction =
  | 'started'
  | 'stopped'
  | 'changed'
  | 'missed'
  | 'refilled'
  | 'side_effect'
  | 'other'

export interface MedicationDetails {
  medication_name: string
  dose_text: string | null
  action: MedicationAction
  prescriber: string | null
}

export type NoteCategory = 'logistics' | 'mood' | 'finance' | 'equipment' | 'admin' | 'other'

export interface NoteDetails {
  category: NoteCategory
}

export type SymptomEvent = EventBase & { kind: 'symptom'; details: SymptomDetails }
export type AppointmentEvent = EventBase & { kind: 'appointment'; details: AppointmentDetails }
export type MedicationEvent = EventBase & { kind: 'medication'; details: MedicationDetails }
export type NoteEvent = EventBase & { kind: 'note'; details: NoteDetails }

/**
 * The discriminated union the API returns. The LLM emits ONE FLAT model instead (strict
 * structured outputs reject discriminators); the server folds the flat fields into `details`.
 * Shaping the response this way is what makes `switch (event.kind)` exhaustive.
 */
export type Event = SymptomEvent | AppointmentEvent | MedicationEvent | NoteEvent

/** `GET /api/feed`. `next_before` is null when there is no next page. */
export interface FeedPage {
  events: Event[]
  next_before: string | null
}

/** `GET /api/upcoming` — "upcoming" is a query (`occurred_at > now()`), not an entity. */
export interface UpcomingPage {
  events: Event[]
}

export interface Household {
  id: string
  name: string
  care_recipient_name: string
  timezone: string
}

/** `GET /api/me` — the session probe the app boots on. 401 here is normal, not an error. */
export interface Session {
  household: Household
  counts: { events: number; messages: number }
}

/** A WhatsApp participant. Not a login account — there is one shared family credential. */
export interface Member {
  id: string
  display_name: string
  wa_jid: string | null
  wa_lid: string | null
  message_count: number
  first_seen_at: string
  last_seen_at: string
}

export interface PreviewMessage {
  sent_at: string
  sender: string
  text: string
}

/** What the `.txt` parser found, from `POST /api/imports/preview`. Nothing is stored yet. */
export interface ParseReport {
  total_lines: number
  messages: number
  continuations: number
  system_lines: number
  media_placeholders: number
  unparsed_lines: number
  unparsed_samples: string[]
  detected_format: string
  senders: Record<string, number>
  first_sent_at: string
  last_sent_at: string
  preview_head: PreviewMessage[]
  preview_tail: PreviewMessage[]
}

/**
 * Anything other than a clean `day>12`/`month>12` means the UI must make the user choose
 * explicitly: `dd/mm` vs `mm/dd` is undecidable for an export spanning under 12 days, and
 * guessing wrong shifts every date by months with nothing throwing.
 */
export type DayfirstEvidence = 'day>12' | 'month>12' | 'conflict' | 'none'

export interface ImportSniff {
  dayfirst: boolean
  dayfirst_evidence: DayfirstEvidence
  timezone: string
  timezone_source: string
}

export interface ImportEstimate {
  message_count: number
  estimated_cost_usd: string
  estimated_minutes: number
  budget_usd: string
  over_budget: boolean
}

export interface ImportPreview {
  filename: string
  file_sha256: string
  report: ParseReport
  sniffed: ImportSniff
  estimate: ImportEstimate
}

export interface ImportAccepted {
  import_id: string
  message_count: number
  estimated_cost_usd: string
}

export type ImportState = 'pending' | 'importing' | 'extracting' | 'complete' | 'failed'

/** `GET /api/imports/{id}`. Poll at 2s; `complete` and `failed` are terminal. */
export interface ImportStatus {
  status: ImportState
  message_count: number
  inserted_count: number
  extracted_count: number
  error: string | null
}

export type ReportState = 'pending' | 'running' | 'complete' | 'failed'

/** `GET /api/reports` — the list shape. `generated_at` is null until complete. */
export interface ReportSummary {
  id: string
  title: string
  period_start: string
  period_end: string
  status: ReportState
  generated_at: string | null
}

/** Citations are resolved server-side; a handle that didn't resolve never reaches the client. */
export interface ReportCitation {
  handle: string
  event_id: string
  kind: EventKind
  occurred_at: string
  title: string
}

export interface ReportSection {
  heading: string
  body_markdown: string
  citations: ReportCitation[]
}

export interface Report extends ReportSummary {
  summary: string
  urgent_flag: boolean
  urgent_reason: string | null
  sections: ReportSection[]
  questions_for_the_doctor: string[]
  watch_items: string[]
  data_gaps: string[]
  error: string | null
}

/**
 * Exhaustiveness guard. Call it in the default arm of a `switch` over a union: adding a fifth
 * event kind then fails `tsc -b` at every render site instead of silently rendering a blank card.
 */
export function assertNever(value: never): never {
  throw new Error(`Unhandled union member: ${JSON.stringify(value)}`)
}
