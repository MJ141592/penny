/**
 * Every react-query hook the app uses. Re-exports the keys from `./keys`, so an invalidation and
 * the query it is meant to hit cannot drift apart.
 */

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type InfiniteData,
  type QueryClient,
  type UseMutationResult,
} from '@tanstack/react-query'

import type {
  Event,
  FeedPage,
  ImportState,
  ImportStatus,
  Session,
  UpcomingPage,
  WhatsappRelink,
} from '../types/api'
import { ApiError } from './client'
import {
  changePassword,
  deleteEvent,
  getFeed,
  getImportStatus,
  getMembers,
  getReport,
  getReports,
  getSession,
  getUpcoming,
  getWhatsappStatus,
  linkWhatsappGroup,
  login,
  logout,
  mergeMember,
  patchEvent,
  patchHousehold,
  previewImport,
  requestWhatsappRelink,
  startImport,
  type EventPatch,
  type HouseholdPatch,
} from './endpoints'
import { queryKeys } from './keys'

export { queryKeys }

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

export function useChangePassword(): UseMutationResult<
  void,
  Error,
  { current_password: string; new_password: string }
> {
  return useMutation({
    mutationFn: ({ current_password, new_password }) =>
      changePassword(current_password, new_password),
  })
}

/* ---------------------------------------------------------------- events, optimistically ---- */

type FeedCache = InfiniteData<FeedPage, string | undefined> | undefined

/** What `onMutate` hands `onError` so a failed write can put the screen back exactly as it was. */
interface EventRollback {
  feed: FeedCache
  upcoming: UpcomingPage | undefined
}

function invalidateEventViews(client: QueryClient) {
  void client.invalidateQueries({ queryKey: queryKeys.feed })
  void client.invalidateQueries({ queryKey: queryKeys.upcoming })
}

/**
 * Snapshot both event views and stop their in-flight refetches.
 *
 * `cancelQueries` is not optional here: the feed polls every 60s, so a refetch launched a moment
 * before the edit would land afterwards carrying the *old* row and silently undo the optimistic
 * change on screen — which reads as "my edit didn't save" and gets typed in again.
 */
async function takeEventSnapshot(client: QueryClient): Promise<EventRollback> {
  await client.cancelQueries({ queryKey: queryKeys.feed })
  await client.cancelQueries({ queryKey: queryKeys.upcoming })
  return {
    feed: client.getQueryData<InfiniteData<FeedPage, string | undefined>>(queryKeys.feed),
    upcoming: client.getQueryData<UpcomingPage>(queryKeys.upcoming),
  }
}

function restoreEventSnapshot(client: QueryClient, snapshot: EventRollback | undefined) {
  if (!snapshot) return
  client.setQueryData(queryKeys.feed, snapshot.feed)
  client.setQueryData(queryKeys.upcoming, snapshot.upcoming)
}

/** Applies `edit` to every cached copy of one event, across all feed pages and the panel. */
function writeEventEverywhere(
  client: QueryClient,
  id: string,
  edit: (event: Event) => Event | null,
) {
  const apply = (events: Event[]): Event[] =>
    events.flatMap((event) => {
      if (event.id !== id) return [event]
      const next = edit(event)
      return next ? [next] : []
    })

  client.setQueryData<InfiniteData<FeedPage, string | undefined>>(queryKeys.feed, (old) =>
    old ? { ...old, pages: old.pages.map((page) => ({ ...page, events: apply(page.events) })) } : old,
  )
  client.setQueryData<UpcomingPage>(queryKeys.upcoming, (old) =>
    old ? { ...old, events: apply(old.events) } : old,
  )
}

/**
 * The edit lands on screen before the request does, and is put back if the request fails.
 *
 * `edited_at` is set optimistically too, because it is what renders the "edited" marker — and it
 * is not cosmetic: it is the flag that permanently protects a human's correction from being
 * overwritten by a later re-extraction. Seeing it appear is the confirmation that the edit stuck.
 */
export function useUpdateEvent(): UseMutationResult<
  Event,
  Error,
  { id: string; patch: EventPatch },
  EventRollback
> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, patch }) => patchEvent(id, patch),
    onMutate: async ({ id, patch }) => {
      const snapshot = await takeEventSnapshot(client)
      const { details, ...rest } = patch
      writeEventEverywhere(client, id, (event) => {
        const cleaned = Object.fromEntries(
          Object.entries(rest).filter(([, value]) => value !== undefined),
        )
        return {
          ...event,
          ...cleaned,
          // `details` MERGES, matching the server: the form sends only the keys it changed.
          details: { ...event.details, ...details },
          edited_at: new Date().toISOString(),
        } as Event
      })
      return snapshot
    },
    onError: (_error, _variables, snapshot) => restoreEventSnapshot(client, snapshot),
    // Refetch either way: the server owns `occurred_at` re-sorting and the real `edited_at`.
    onSettled: () => invalidateEventViews(client),
  })
}

/** Soft delete server-side; here it just leaves, and comes back if the request didn't. */
export function useDeleteEvent(): UseMutationResult<void, Error, string, EventRollback> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteEvent(id),
    onMutate: async (id) => {
      const snapshot = await takeEventSnapshot(client)
      writeEventEverywhere(client, id, () => null)
      return snapshot
    },
    onError: (_error, _id, snapshot) => restoreEventSnapshot(client, snapshot),
    onSettled: () => invalidateEventViews(client),
  })
}

/* ------------------------------------------------------------------------------ whatsapp ---- */

/**
 * Poll at 30s, and only where it is on screen — this proxies a request to GOWA, so it is the one
 * query in the app that costs something outside our own process.
 */
export function useWhatsappStatus(enabled = true) {
  return useQuery({
    queryKey: queryKeys.whatsapp,
    queryFn: ({ signal }) => getWhatsappStatus(signal),
    enabled,
    refetchInterval: 30_000,
  })
}

export function useLinkWhatsappGroup(): UseMutationResult<void, Error, string> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (groupExternalId: string) => linkWhatsappGroup(groupExternalId),
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.whatsapp }),
  })
}

/**
 * A mutation and not a query, deliberately: it takes up to two minutes and produces a PNG that
 * expires in about thirty seconds, so it must fire when a person asks for it and never on a poll
 * or a remount.
 */
export function useWhatsappRelink(): UseMutationResult<WhatsappRelink, Error, void> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: requestWhatsappRelink,
    // The session usually comes up within a few seconds of the scan; go and look.
    onSuccess: () => client.invalidateQueries({ queryKey: queryKeys.whatsapp }),
  })
}

/**
 * Merging re-points messages and events, so the feed's attribution changes underneath it — both
 * event views have to be refetched, not just the member list.
 */
export function useMergeMember(): UseMutationResult<
  void,
  Error,
  { id: string; into_member_id: string }
> {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, into_member_id }) => mergeMember(id, into_member_id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.members })
      invalidateEventViews(client)
    },
  })
}

/* -------------------------------------------------------------------------------- imports ---- */

export function usePreviewImport() {
  return useMutation({ mutationFn: (file: File) => previewImport(file) })
}

export function useStartImport() {
  return useMutation({
    mutationFn: ({ file, dayfirst, timezone }: { file: File; dayfirst: boolean; timezone: string }) =>
      startImport(file, dayfirst, timezone),
  })
}
