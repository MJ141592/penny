/**
 * The offline backend. Answers with a real `Response`, so `api/client.ts` has exactly one
 * parsing and error path and the fixture/real split is a single `if`.
 *
 * It is a small server, not a lookup table, on purpose: it enforces the session, honours
 * `?limit`/`?before`, derives `/upcoming` from the same events the feed serves, and mutates in
 * place on PATCH/DELETE. That is what makes the 401 redirect, the pagination, the "edited"
 * marker and the import poller real work rather than props waiting for M4.
 *
 * Scenario switch: append `?scenario=empty` to the page URL to serve the empty household, which
 * is how the empty states get exercised without a second build.
 */

import type {
  Event,
  FeedPage,
  Household,
  ImportAccepted,
  ImportStatus,
  Report,
  ReportSummary,
  Session,
} from '../types/api'

import type { Loosen } from './contract'

import emptyFeed from './feed-empty.json'
import feed from './feed.json'
import importPreview from './import-preview.json'
import me from './me.json'
import members from './members.json'
import reports from './reports.json'

// TypeScript widens JSON string literals to `string`, so an imported fixture is never assignable
// to a discriminated union however correct it is. The cast is unavoidable and lives here once —
// but it goes through `Loosen<...>` first, which still checks every field name, every
// nullability and every per-kind `details` shape against docs/api-contract.md at compile time.
// A bare `as unknown as` checks nothing at all. See ./contract.ts.
const CHECKED_EVENTS: Loosen<Event>[] = feed.events
const CHECKED_REPORTS: Loosen<Report>[] = reports

const ALL_EVENTS = CHECKED_EVENTS as unknown as Event[]
const ALL_REPORTS = CHECKED_REPORTS as unknown as Report[]

const SCENARIO = new URLSearchParams(globalThis.location?.search ?? '').get('scenario')

const DEMO_USERNAME = 'the-doyles'
const DEMO_PASSWORD = 'correct-horse-battery-staple'

/** Enough delay that loading states are visible while developing, not enough to be annoying. */
const LATENCY_MS = 140

// Mutable so edits and deletes survive a navigation the way the real backend would.
let events: Event[] = structuredClone(SCENARIO === 'empty' ? (emptyFeed.events as Event[]) : ALL_EVENTS)
let household: Household = { ...me.household }
let signedIn = true
let importStartedAt: number | null = null

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function noContent(): Response {
  return new Response(null, { status: 204 })
}

/** Every non-2xx body is `{"detail": "..."}` — same envelope the backend promises. */
function problem(status: number, detail: string): Response {
  return json({ detail }, status)
}

/** 404, never 403: a 403 would confirm the row exists somewhere. */
function notFound(): Response {
  return problem(404, 'That could not be found.')
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function readJsonBody(init: RequestInit): Promise<Record<string, unknown>> {
  if (typeof init.body !== 'string') return {}
  try {
    const parsed: unknown = JSON.parse(init.body)
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

function summarise(report: Report): ReportSummary {
  const { id, title, period_start, period_end, status, generated_at } = report
  return { id, title, period_start, period_end, status, generated_at }
}

/** Mirrors the real progression so the 2s poller has something to stop on. */
function importStatus(): ImportStatus {
  const total = 2114
  if (importStartedAt === null) {
    return { status: 'pending', message_count: total, inserted_count: 0, extracted_count: 0, error: null }
  }
  const elapsed = Date.now() - importStartedAt
  if (elapsed < 2_000) {
    return { status: 'importing', message_count: total, inserted_count: 0, extracted_count: 0, error: null }
  }
  if (elapsed < 12_000) {
    const progress = (elapsed - 2_000) / 10_000
    return {
      status: 'extracting',
      message_count: total,
      inserted_count: total,
      extracted_count: Math.round(total * progress),
      error: null,
    }
  }
  return {
    status: 'complete',
    message_count: total,
    inserted_count: total,
    extracted_count: total,
    error: null,
  }
}

function handleFeed(query: URLSearchParams): Response {
  const limit = Math.min(Number(query.get('limit') ?? 200), 500)
  const before = query.get('before')
  const visible = before ? events.filter((event) => event.occurred_at < before) : events
  const page = visible.slice(0, limit)
  const last = page.at(-1)
  const body: FeedPage = {
    events: page,
    // Null when a short page came back — that is the client's signal to stop.
    next_before: page.length < limit ? null : (last?.occurred_at ?? null),
  }
  return json(body)
}

function handleUpcoming(): Response {
  const now = new Date().toISOString()
  const upcoming = events
    .filter((event) => event.occurred_at > now)
    .sort((a, b) => a.occurred_at.localeCompare(b.occurred_at))
    .slice(0, 50)
  return json({ events: upcoming })
}

async function handlePatchEvent(id: string, init: RequestInit): Promise<Response> {
  const index = events.findIndex((event) => event.id === id)
  const current = events[index]
  if (!current) return notFound()

  const patch = await readJsonBody(init)
  const { details, ...rest } = patch
  // `details` merges rather than replaces: the client sends only the keys it is changing.
  const merged = {
    ...current,
    ...rest,
    details: { ...current.details, ...(details as object | undefined) },
    edited_at: new Date().toISOString(),
  } as Event
  events[index] = merged
  return json(merged)
}

async function respond(method: string, path: string, query: URLSearchParams, init: RequestInit) {
  const [head, second] = path.split('/').filter(Boolean)

  if (method === 'POST' && head === 'auth' && second === 'login') {
    const body = await readJsonBody(init)
    if (body.username !== DEMO_USERNAME || body.password !== DEMO_PASSWORD) {
      // Identical for an unknown username and a wrong password — no user enumeration.
      return problem(401, 'Invalid username or password.')
    }
    signedIn = true
    return noContent()
  }

  // Everything below needs the session cookie. 401 is the client's cue to show /login.
  if (!signedIn) return problem(401, 'Not signed in.')

  if (method === 'POST' && head === 'auth' && second === 'logout') {
    signedIn = false
    return noContent()
  }

  if (method === 'GET' && head === 'me') {
    const session: Session = {
      household,
      counts: { events: events.length, messages: SCENARIO === 'empty' ? 0 : me.counts.messages },
    }
    return json(session)
  }

  if (method === 'PATCH' && head === 'household') {
    const patch = await readJsonBody(init)
    household = { ...household, ...patch }
    return json(household)
  }

  if (method === 'GET' && head === 'feed') return handleFeed(query)
  if (method === 'GET' && head === 'upcoming') return handleUpcoming()
  if (method === 'GET' && head === 'members') return json(members)

  if (head === 'events' && second) {
    if (method === 'PATCH') return handlePatchEvent(second, init)
    if (method === 'DELETE') {
      const before = events.length
      events = events.filter((event) => event.id !== second)
      return events.length === before ? notFound() : noContent()
    }
  }

  if (method === 'POST' && head === 'reports' && second === 'generate') {
    return problem(429, 'A report was already generated today.')
  }
  if (method === 'GET' && head === 'reports') {
    if (!second) return json(ALL_REPORTS.map(summarise))
    const report = ALL_REPORTS.find((candidate) => candidate.id === second)
    return report ? json(report) : notFound()
  }

  if (method === 'POST' && head === 'imports' && second === 'preview') return json(importPreview)
  if (method === 'POST' && head === 'imports' && !second) {
    importStartedAt = Date.now()
    const accepted: ImportAccepted = {
      import_id: 'b21e4f77-0f2c-4c9e-9a01-88c4d1b2c3f4',
      message_count: importPreview.estimate.message_count,
      estimated_cost_usd: importPreview.estimate.estimated_cost_usd,
    }
    return json(accepted, 202)
  }
  if (method === 'GET' && head === 'imports' && second) return json(importStatus())

  return notFound()
}

/** Same signature contract as `fetch`: resolves with a `Response`, never throws on 4xx. */
export async function fixtureRespond(url: string, init: RequestInit): Promise<Response> {
  await sleep(LATENCY_MS)
  const [path = '/', search = ''] = url.split('?')
  return respond(init.method ?? 'GET', path, new URLSearchParams(search), init)
}
