/**
 * The single door every HTTP call goes through.
 *
 * One place, so that the fixture/real split is one `if` rather than an edit at every call site.
 * The fixture layer answers with a real `Response`, so both modes share one parsing and error
 * path — the code that runs in the demo is the code that runs in production, minus the transport.
 */

import { fixtureRespond } from '../fixtures'

/**
 * Default FALSE: the backend exists, so every build talks to it unless somebody deliberately asks
 * for the offline demo with `VITE_USE_FIXTURES=true`. Opt-in rather than opt-out, because the
 * failure mode of the old default was silent — a deploy that forgot the env var would have served
 * one invented family's health history to everybody and looked completely fine doing it.
 *
 * Vite inlines `import.meta.env` at build time, so this is a compile-time constant.
 */
export const USE_FIXTURES = import.meta.env.VITE_USE_FIXTURES === 'true'

export const API_BASE = '/api'

/**
 * Every non-2xx body is `{"detail": "one sentence, safe to render"}` — the backend flattens
 * FastAPI's list-shaped validation errors — so callers can render `error.message` unconditionally
 * and never type-narrow. `status` is carried because the retry policy and the 401 redirect both
 * key on it.
 */
export class ApiError extends Error {
  readonly status: number
  /**
   * The path it came from, carried because a 401 does not mean the same thing everywhere. On
   * `/auth/login` and `/household/password` a 401 is the answer to a password in the *body*
   * ("that is not your password"); anywhere else it means the session cookie is gone. Without
   * this, mistyping your current password on the settings screen signs you out — which is both
   * baffling and loses whatever you were typing.
   */
  readonly path: string

  constructor(status: number, detail: string, path = '') {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.path = path
  }

  /** 401 is "you are not signed in" — the cue to redirect to /login, never to retry. */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  /** True when the 401 answers a password we sent, rather than a cookie we didn't. */
  get isCredentialCheck(): boolean {
    return CREDENTIAL_PATHS.some((candidate) => this.path.startsWith(candidate))
  }
}

const CREDENTIAL_PATHS = ['/auth/login', '/household/password']

export interface ApiRequest {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  /** JSON body. Mutually exclusive with `form`. */
  json?: unknown
  /** multipart/form-data body, for the two import routes. */
  form?: FormData
  /** Undefined and null values are dropped rather than sent as the string "undefined". */
  query?: Record<string, string | number | boolean | null | undefined>
  signal?: AbortSignal
}

function withQuery(path: string, query: ApiRequest['query']): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

async function readDetail(response: Response): Promise<string> {
  // A 5xx from a proxy, or an HTML error page, will not be JSON. Never let the parse failure
  // replace the real status with a confusing "Unexpected token <".
  try {
    const body: unknown = await response.json()
    if (body && typeof body === 'object' && 'detail' in body) {
      const detail = (body as { detail: unknown }).detail
      if (typeof detail === 'string') return detail
    }
  } catch {
    /* fall through to the generic sentence */
  }
  return response.status >= 500
    ? 'Something went wrong on our side. Try again in a moment.'
    : 'That request could not be completed.'
}

/** `T` is `void` for 204 routes; the body is not parsed. */
export async function api<T>(path: string, request: ApiRequest = {}): Promise<T> {
  const { method = 'GET', json, form, query, signal } = request
  const url = withQuery(path, query)

  const init: RequestInit = {
    method,
    // Same-origin in production, so the signed HttpOnly session cookie rides along with no CORS
    // and no CSRF token. Set explicitly anyway: the Vite dev proxy is a different origin.
    credentials: 'include',
    signal: signal ?? null,
  }
  if (form) {
    init.body = form
  } else if (json !== undefined) {
    init.body = JSON.stringify(json)
    init.headers = { 'Content-Type': 'application/json' }
  }

  const response = USE_FIXTURES
    ? await fixtureRespond(url, init)
    : await fetch(`${API_BASE}${url}`, init)

  if (!response.ok) throw new ApiError(response.status, await readDetail(response), path)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}
