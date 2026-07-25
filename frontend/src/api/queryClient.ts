import { QueryClient } from '@tanstack/react-query'

import { ApiError } from './client'

export const queryClient = new QueryClient({
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
