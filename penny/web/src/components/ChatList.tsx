"use client";

import { useMemo } from "react";
import type { Chat } from "@/lib/types";
import { isGroup, phoneOf } from "@/lib/types";

function relativeTime(iso: string): string {
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return "";
  const mins = Math.floor((Date.now() - t) / 60_000);
  if (mins < 1) return "now";
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d`;
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export default function ChatList({
  chats,
  selected,
  onSelect,
  query,
  onQuery,
  unread,
  loading,
}: {
  chats: Chat[];
  selected?: string;
  onSelect: (jid: string) => void;
  query: string;
  onQuery: (q: string) => void;
  unread: Record<string, number>;
  loading: boolean;
}) {
  const sorted = useMemo(
    () =>
      [...chats].sort(
        (a, b) =>
          new Date(b.last_message_time).getTime() -
          new Date(a.last_message_time).getTime(),
      ),
    [chats],
  );

  return (
    <aside className="flex h-full w-80 shrink-0 flex-col border-r border-[var(--border)] bg-[var(--bg-sunken)]">
      <div className="border-b border-[var(--border)] p-3">
        <input
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search chats"
          className="w-full rounded-lg border border-[var(--border)] bg-[var(--bg-raised)] px-3 py-2 text-sm outline-none placeholder:text-[var(--text-dim)] focus:border-[var(--accent)]"
        />
      </div>

      <div className="scroll-thin flex-1 overflow-y-auto">
        {loading && sorted.length === 0 ? (
          <p className="p-4 text-sm text-[var(--text-dim)]">Loading chats…</p>
        ) : sorted.length === 0 ? (
          <p className="p-4 text-sm text-[var(--text-dim)]">
            No chats yet. History syncs in over the first minute or two after linking.
          </p>
        ) : (
          sorted.map((c) => {
            const group = isGroup(c.jid);
            const active = c.jid === selected;
            const badge = unread[c.jid] ?? 0;
            return (
              <button
                key={c.jid}
                onClick={() => onSelect(c.jid)}
                className={`flex w-full items-center gap-3 border-b border-[var(--border)]/60 px-3 py-3 text-left transition-colors ${
                  active ? "bg-[var(--bg-raised)]" : "hover:bg-[var(--bg-raised)]/60"
                }`}
              >
                <span
                  className={`grid h-9 w-9 shrink-0 place-items-center rounded-full text-xs font-semibold ${
                    group
                      ? "bg-[var(--accent)] text-[var(--accent-fg)]"
                      : "bg-[var(--border)] text-[var(--text-dim)]"
                  }`}
                >
                  {group ? "GR" : (c.name || phoneOf(c.jid)).slice(0, 2).toUpperCase()}
                </span>

                <span className="min-w-0 flex-1">
                  <span className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-sm font-medium">
                      {c.name || phoneOf(c.jid)}
                    </span>
                    <span className="shrink-0 text-[11px] text-[var(--text-dim)]">
                      {relativeTime(c.last_message_time)}
                    </span>
                  </span>
                  <span className="mt-0.5 flex items-center justify-between gap-2">
                    <span className="truncate text-xs text-[var(--text-dim)]">
                      {group ? "Group" : phoneOf(c.jid)}
                    </span>
                    {badge > 0 && (
                      <span className="shrink-0 rounded-full bg-[var(--accent)] px-1.5 py-0.5 text-[10px] font-semibold text-[var(--accent-fg)]">
                        {badge}
                      </span>
                    )}
                  </span>
                </span>
              </button>
            );
          })
        )}
      </div>
    </aside>
  );
}
