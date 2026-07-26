"use client";

import { Fragment, useEffect, useRef } from "react";
import type { ChatMessage, GroupParticipant } from "@/lib/types";
import { isGroup, phoneOf } from "@/lib/types";

/** Renders @<digits> as a highlighted token, resolving to a display name when known. */
function renderContent(text: string, nameByPhone: Map<string, string>) {
  const parts = text.split(/(@\d{5,})/g);
  return parts.map((part, i) => {
    if (/^@\d{5,}$/.test(part)) {
      const phone = part.slice(1);
      return (
        <span key={i} className="mention-token">
          @{nameByPhone.get(phone) ?? phone}
        </span>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

function dayLabel(iso: string) {
  const d = new Date(iso);
  const today = new Date();
  const yest = new Date(Date.now() - 86_400_000);
  const same = (a: Date, b: Date) => a.toDateString() === b.toDateString();
  if (same(d, today)) return "Today";
  if (same(d, yest)) return "Yesterday";
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: d.getFullYear() === today.getFullYear() ? undefined : "numeric",
  });
}

export default function Thread({
  jid,
  messages,
  participants,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  jid: string;
  messages: ChatMessage[];
  participants: GroupParticipant[];
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const prevCount = useRef(0);
  const prevJid = useRef(jid);

  const nameByPhone = new Map<string, string>();
  for (const p of participants) {
    const phone = phoneOf(p.JID ?? p.jid ?? p.PhoneNumber);
    if (phone && p.DisplayName) nameByPhone.set(phone, p.DisplayName);
  }

  // Jump to the newest message on chat switch or when a message arrives,
  // but hold position when older history is prepended.
  useEffect(() => {
    const switched = prevJid.current !== jid;
    const appended = messages.length > prevCount.current && !switched;
    prevJid.current = jid;

    if (switched || appended) {
      bottomRef.current?.scrollIntoView({ block: "end" });
    }
    prevCount.current = messages.length;
  }, [jid, messages]);

  // Precompute separators so the map below stays a pure projection.
  const rows = messages.map((m, i) => {
    const day = dayLabel(m.timestamp);
    const prev = i > 0 ? dayLabel(messages[i - 1].timestamp) : null;
    return { message: m, day, showDay: day !== prev };
  });

  return (
    <div ref={scrollRef} className="scroll-thin flex-1 overflow-y-auto px-4 py-4">
      {hasMore && (
        <div className="mb-4 flex justify-center">
          <button
            onClick={onLoadMore}
            disabled={loadingMore}
            className="rounded-full border border-[var(--border)] bg-[var(--bg-raised)] px-4 py-1.5 text-xs text-[var(--text-dim)] transition-colors hover:border-[var(--accent)] hover:text-[var(--text)] disabled:opacity-50"
          >
            {loadingMore ? "Loading…" : "Load earlier messages"}
          </button>
        </div>
      )}

      {messages.length === 0 && (
        <p className="mt-10 text-center text-sm text-[var(--text-dim)]">
          No messages in this chat yet.
        </p>
      )}

      {rows.map(({ message: m, day, showDay }) => {
        const senderPhone = phoneOf(m.sender_jid);
        const senderName =
          m.pushname ?? nameByPhone.get(senderPhone) ?? senderPhone;

        return (
          <Fragment key={m.id}>
            {showDay && (
              <div className="my-4 flex justify-center">
                <span className="rounded-full bg-[var(--bg-sunken)] px-3 py-1 text-[11px] text-[var(--text-dim)]">
                  {day}
                </span>
              </div>
            )}
            <div
              className={`mb-2 flex ${m.is_from_me ? "justify-end" : "justify-start"}`}
            >
              <div
                className={`max-w-[68%] rounded-2xl px-3 py-2 ${
                  m.is_from_me
                    ? "rounded-br-sm bg-[var(--bubble-out)]"
                    : "rounded-bl-sm bg-[var(--bubble-in)]"
                }`}
              >
                {isGroup(jid) && !m.is_from_me && (
                  <div className="mb-0.5 text-[11px] font-semibold text-[var(--accent)]">
                    {senderName}
                  </div>
                )}

                {m.media_type && (
                  <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--text-dim)]">
                    {m.media_type}
                    {m.filename ? ` · ${m.filename}` : ""}
                  </div>
                )}

                <div className="whitespace-pre-wrap break-words text-sm">
                  {m.content ? (
                    renderContent(m.content, nameByPhone)
                  ) : (
                    <span className="italic text-[var(--text-dim)]">
                      {m.media_type ? "(media)" : "(no text)"}
                    </span>
                  )}
                </div>

                <div className="mt-1 flex items-center justify-end gap-2">
                  {m.reactions && m.reactions.length > 0 && (
                    <span className="text-xs">
                      {m.reactions.map((r) => r.emoji).join(" ")}
                    </span>
                  )}
                  <span className="text-[10px] text-[var(--text-dim)]">
                    {new Date(m.timestamp).toLocaleTimeString(undefined, {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                </div>
              </div>
            </div>
          </Fragment>
        );
      })}
      <div ref={bottomRef} />
    </div>
  );
}
