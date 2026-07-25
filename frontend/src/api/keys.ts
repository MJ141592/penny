/**
 * Query keys live alone so that both `queries.ts` (which reads and invalidates them) and
 * `queryClient.ts` (which has to blank the session on a 401 from *any* query) can import them
 * without an import cycle.
 */

export const queryKeys = {
  session: ['session'] as const,
  feed: ['feed'] as const,
  upcoming: ['upcoming'] as const,
  members: ['members'] as const,
  reports: ['reports'] as const,
  whatsapp: ['whatsapp'] as const,
  report: (id: string) => ['reports', id] as const,
  importStatus: (id: string) => ['imports', id] as const,
}
