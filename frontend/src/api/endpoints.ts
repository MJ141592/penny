/**
 * One typed function per route in `docs/api-contract.md`. Nothing here knows about fixtures or
 * react-query — it is just the contract, expressed once, so a path or param typo is a compile
 * error at exactly one place instead of a 404 at runtime in five.
 */

import type {
  Event,
  EventCreate,
  FeedPage,
  Household,
  ImportAccepted,
  ImportPreview,
  ImportStatus,
  Member,
  OccurredAtPrecision,
  Report,
  ReportSummary,
  Session,
  UpcomingPage,
  WhatsappRelink,
  WhatsappStatus,
} from '../types/api'
import { api } from './client'

export function getSession(signal?: AbortSignal): Promise<Session> {
  return api('/me', { signal })
}

export function login(username: string, password: string): Promise<void> {
  return api('/auth/login', { method: 'POST', json: { username, password } })
}

export function logout(): Promise<void> {
  return api('/auth/logout', { method: 'POST' })
}

export type HouseholdPatch = Partial<Pick<Household, 'name' | 'care_recipient_name' | 'timezone'>>

export function patchHousehold(patch: HouseholdPatch): Promise<Household> {
  return api('/household', { method: 'PATCH', json: patch })
}

/**
 * Existing sessions survive deliberately: the cookie carries `household_id`, not the password, so
 * changing it does not sign the rest of the family out mid-conversation. Re-sharing the new
 * passphrase is a human job.
 */
export function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  return api('/household/password', {
    method: 'POST',
    json: { current_password: currentPassword, new_password: newPassword },
  })
}

export function getWhatsappStatus(signal?: AbortSignal): Promise<WhatsappStatus> {
  return api('/whatsapp/status', { signal })
}

/** 400 when the id isn't a group: there is no `is_group` flag, the `@g.us` suffix is the signal. */
export function linkWhatsappGroup(groupExternalId: string): Promise<void> {
  return api('/whatsapp/link', { method: 'POST', json: { group_external_id: groupExternalId } })
}

/**
 * Ask the bridge for a fresh pairing QR. **This blocks for up to two minutes** — the bridge waits
 * for whatsmeow to produce the code — so it is a button with a spinner, never a poll, and never
 * shares a query with `/whatsapp/status`. 409 means one request is already in flight.
 */
export function requestWhatsappRelink(): Promise<WhatsappRelink> {
  return api('/whatsapp/relink', { method: 'POST' })
}

/**
 * `id` is absorbed into `intoMemberId`: messages and events re-point and the survivor keeps
 * whichever of `wa_jid`/`wa_lid` is non-null. Attribution is fixed retroactively, with no LLM run.
 */
export function mergeMember(id: string, intoMemberId: string): Promise<void> {
  return api(`/members/${id}/merge`, { method: 'POST', json: { into_member_id: intoMemberId } })
}

export interface FeedQuery {
  limit: number
  /** Exclusive on `occurred_at`. Omit for the first page. */
  before?: string
  signal?: AbortSignal
}

export function getFeed({ limit, before, signal }: FeedQuery): Promise<FeedPage> {
  return api('/feed', { query: { limit, before }, signal })
}

export function getUpcoming(signal?: AbortSignal): Promise<UpcomingPage> {
  return api('/upcoming', { signal })
}

export function createEvent(create: EventCreate): Promise<Event> {
  return api('/events', { method: 'POST', json: create })
}

/** `details` is merged server-side, so send only the keys being changed. */
export interface EventPatch {
  title?: string
  body?: string | null
  occurred_at?: string
  occurred_at_precision?: OccurredAtPrecision
  details?: Record<string, unknown>
}

export function patchEvent(id: string, patch: EventPatch): Promise<Event> {
  return api(`/events/${id}`, { method: 'PATCH', json: patch })
}

export function deleteEvent(id: string): Promise<void> {
  return api(`/events/${id}`, { method: 'DELETE' })
}

export function getReports(signal?: AbortSignal): Promise<ReportSummary[]> {
  return api('/reports', { signal })
}

export function getReport(id: string, signal?: AbortSignal): Promise<Report> {
  return api(`/reports/${id}`, { signal })
}

export function createReport(periodDays = 30): Promise<Report> {
  return api('/reports', { method: 'POST', json: { period_days: periodDays } })
}

export function getMembers(signal?: AbortSignal): Promise<Member[]> {
  return api('/members', { signal })
}

export function previewImport(file: File): Promise<ImportPreview> {
  const form = new FormData()
  form.set('file', file)
  return api('/imports/preview', { method: 'POST', form })
}

/**
 * `dayfirst` and `timezone` are required with no server-side default: `dd/mm` vs `mm/dd` is
 * undecidable for a short export, and guessing wrong shifts every date by months, silently.
 */
export function startImport(file: File, dayfirst: boolean, timezone: string): Promise<ImportAccepted> {
  const form = new FormData()
  form.set('file', file)
  form.set('dayfirst', String(dayfirst))
  form.set('timezone', timezone)
  return api('/imports', { method: 'POST', form })
}

export function getImportStatus(id: string, signal?: AbortSignal): Promise<ImportStatus> {
  return api(`/imports/${id}`, { signal })
}
