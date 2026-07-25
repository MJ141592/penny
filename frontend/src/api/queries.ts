/**
 * Every react-query hook the app uses. Keys live here too, so an invalidation and the query it
 * is meant to hit cannot drift apart.
 */

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
} from '@tanstack/react-query'

import type { Event, ImportState, ImportStatus, Session } from '../types/api'
import { ApiError } from './client'
import {
  deleteEvent,
  getFeed,
  getImportStatus,
  getMembers,
  getReport,
  getReports,
  getSession,
  getUpcoming,
  login,
  logout,
  patchEvent,
  patchHousehold,
  previewImport,
  startImport,
  type EventPatch,
  type HouseholdPatch,
} from './endpoints'

export const queryKeys = {
  session: ['session'] as const,
  feed: ['feed'] as const,
  upcoming: ['upcoming'] as const,
  members: ['members'] as const,
  reports: ['reports'] as const,
  report: (id: string) => ['reports', id] as const,
  importStatus: (id: string) => ['imports', id] as const,
}

/** Small enough that the demo's ~40 events actually paginate, which is the point of testing it. */
export const FEED_PAGE_SIZE = 25

/**
 * `null` means signed out, which is the normal first-load state and not an error to report.
 * Only a 401 maps to null; a 500 still surfaces so we don't send people to a login screen for
 * a backend outage they can do nothing about.
 */
export function useSession() {
  return useQuery({
    queryKey: queryKeys.session,
    queryFn: async ({ signal }): Promise<Session | null> => {
      try {
        return await getSession(signal)
      } catch (error) {
        if (error instanceof ApiError && error.isUnauthenticated) return null
        throw error
      }
    },
    staleTime: 5 * 60_000,
  })
}

/**
 * The household, or null when signed out. Every screen needs `timezone` to render a date, and
 * this reads the already-cached session rather than threading it through props.
 */
export function useHousehold() {
  return useSession().data?.household ?? null
}

export function useFeed() {
  return useInfiniteQuery({
    queryKey: queryKeys.feed,
    queryFn: ({ pageParam, signal }) => getFeed({ limit: FEED_PAGE_SIZE, before: pageParam, signal }),
    initialPageParam: undefined as string | undefined,
    // `next_before` is null on the last page; undefined is react-query's "no more pages".
    getNextPageParam: (lastPage) => lastPage.next_before ?? undefined,
    refetchInterval: 60_000,
  })
}

export function useUpcoming() {
  return useQuery({
    queryKey: queryKeys.upcoming,
    queryFn: ({ signal }) => getUpcoming(signal),
    refetchInterval: 60_000,
  })
}

export function useMembers() {
  return useQuery({ queryKey: queryKeys.members, queryFn: ({ signal }) => getMembers(signal) })
}

export function useReports() {
  return useQuery({ queryKey: queryKeys.reports, queryFn: ({ signal }) => getReports(signal) })
}

export function useReport(id: string) {
  return useQuery({ queryKey: queryKeys.report(id), queryFn: ({ signal }) => getReport(id, signal) })
}

const TERMINAL_IMPORT_STATES: ImportState[] = ['complete', 'failed']

/** Polls at 2s and stops dead on a terminal status, rather than hammering a finished import. */
export function useImportStatus(importId: string | null) {
  return useQuery({
    queryKey: queryKeys.importStatus(importId ?? 'none'),
    queryFn: ({ signal }) => {
      if (!importId) throw new Error('useImportStatus ran with no import in flight')
      return getImportStatus(importId, signal)
    },
    enabled: importId !== null,
    refetchInterval: (query) =>
      isImportFinished(query.state.data) ? false : 2_000,
  })
}

export function isImportFinished(status: ImportStatus | undefined): boolean {
  return status !== undefined && TERMINAL_IMPORT_STATES.includes(status.status)
}

/**
 * Drop every household's data out of the cache, on both sign-in and sign-out, so the next person
 * on a shared laptop cannot see the previous family's health feed.
 *
 * `resetQueries()` and NOT `queryClient.clear()`, which is what this rule is usually written as.
 * `clear()` deletes the query objects out of the cache, which leaves every mounted `useQuery`
 * holding a detached reference: nothing refetches, so after a successful login the app sits on
 * the login screen forever waiting for a session probe that will never run. `reset()` empties
 * exactly the same data — active and inactive queries alike, verified in `query.reset()` — and
 * then refetches whatever is on screen, which is how the session probe gets re-run.
 */
function forgetCachedHousehold(client: ReturnType<typeof useQueryClient>) {
  void client.resetQueries()
}

export function useLogin(): UseMutationResult<void, Error, { username: string; password: string }> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ username, password }: { username: string; password: string }) =>
      login(username, password),
    // No redirect here: the refetched session lets the layout's gate move us off /login.
    onSuccess: () => forgetCachedHousehold(client),
  })
}

/** `onSettled`, not `onSuccess`: if the request failed the user still asked to leave. */
export function useLogout(): UseMutationResult<void, Error, void> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: logout,
    onSettled: () => forgetCachedHousehold(client),
  })
}

export function useUpdateHousehold() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (patch: HouseholdPatch) => patchHousehold(patch),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.session }),
  })
}

function invalidateEventViews(client: ReturnType<typeof useQueryClient>) {
  void client.invalidateQueries({ queryKey: queryKeys.feed })
  void client.invalidateQueries({ queryKey: queryKeys.upcoming })
}

export function useUpdateEvent(): UseMutationResult<Event, Error, { id: string; patch: EventPatch }> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: EventPatch }) => patchEvent(id, patch),
    onSuccess: () => invalidateEventViews(client),
  })
}

export function useDeleteEvent(): UseMutationResult<void, Error, string> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteEvent(id),
    onSuccess: () => invalidateEventViews(client),
  })
}

export function usePreviewImport() {
  return useMutation({ mutationFn: (file: File) => previewImport(file) })
}

export function useStartImport() {
  return useMutation({
    mutationFn: ({ file, dayfirst, timezone }: { file: File; dayfirst: boolean; timezone: string }) =>
      startImport(file, dayfirst, timezone),
  })
}
