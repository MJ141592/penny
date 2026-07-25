import { MutationCache, QueryCache, QueryClient } from '@tanstack/react-query'

import { ApiError } from './client'
import { queryKeys } from './keys'

/**
 * A 401 from ANY call means the session cookie expired or was cleared, wherever it surfaced.
 *
 * Writing the session query to `null` here — rather than redirecting from thirty call sites — is
 * what makes the layout's gate the single place that decides who sees /login. The session probe
 * already maps its own 401 to `null`; this extends the same rule to the feed poller, a PATCH that
 * fires an hour after the cookie died, and every screen written later by someone who never read
 * this file.
 */
function blankSessionOn401(error: unknown) {
  if (!(error instanceof ApiError) || !error.isUnauthenticated) return
  // A 401 from login or from the change-password form is the answer to a password we just sent,
  // not a dead cookie. Signing somebody out because they mistyped their current password throws
  // away the form they were filling in and looks like a crash.
  if (error.isCredentialCheck) return
  queryClient.setQueryData(queryKeys.session, null)
}

export const queryClient = new QueryClient({
  queryCache: new QueryCache({ onError: blankSessionOn401 }),
  mutationCache: new MutationCache({ onError: blankSessionOn401 }),
  defaultOptions: {
    queries: {
      /**
       * NEVER retry a 4xx. A 401 retried three times just delays the login redirect by several
       * seconds of spinner, and a 422 retried is a guaranteed-identical failure. 5xx is the only
       * class where trying again can plausibly succeed.
       */
      retry: (failureCount, error) =>
        failureCount < 2 && error instanceof ApiError && error.status >= 500,
      staleTime: 30_000,
      // The feed already polls on an interval; refetching on every tab focus on top of that is
      // noise, and health data does not change while the laptop is asleep.
      refetchOnWindowFocus: false,
    },
    mutations: { retry: false },
  },
})
