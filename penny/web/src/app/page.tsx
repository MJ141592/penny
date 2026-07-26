"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ChatList from "@/components/ChatList";
import Composer from "@/components/Composer";
import LoginPanel from "@/components/LoginPanel";
import Thread from "@/components/Thread";
import * as api from "@/lib/client";
import type {
  Chat,
  ChatMessage,
  ConnectionStatus,
  GroupParticipant,
  LiveEvent,
} from "@/lib/types";
import { isGroup, phoneOf } from "@/lib/types";

const PAGE = 50;

export default function Page() {
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [engineDown, setEngineDown] = useState(false);

  const [chats, setChats] = useState<Chat[]>([]);
  const [query, setQuery] = useState("");
  const [chatsLoading, setChatsLoading] = useState(true);

  const [selected, setSelected] = useState<string>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [total, setTotal] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [participants, setParticipants] = useState<GroupParticipant[]>([]);

  const [sending, setSending] = useState(false);
  const [unread, setUnread] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);

  // Mirrors `selected` for the SSE handler, which must not be torn down and
  // rebuilt every time the open chat changes.
  const selectedRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    selectedRef.current = selected;
  }, [selected]);

  /* ---------- connection ---------- */

  const refreshStatus = useCallback(async () => {
    try {
      setStatus(await api.getStatus());
      setEngineDown(false);
    } catch (e) {
      const msg = e instanceof Error ? e.message : "";
      setEngineDown(msg.includes("unreachable") || msg.includes("Engine"));
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    const t = setInterval(refreshStatus, 15_000);
    // Deferred so the first status write lands outside the effect body.
    const first = setTimeout(refreshStatus, 0);
    return () => {
      clearInterval(t);
      clearTimeout(first);
    };
  }, [refreshStatus]);

  const linked = Boolean(status?.is_logged_in);

  /* ---------- chats ---------- */

  const loadChats = useCallback(async (search: string) => {
    setChatsLoading(true);
    try {
      const { data } = await api.listChats({ limit: 100, search });
      setChats(data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load chats");
    } finally {
      setChatsLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!linked) return;
    // Debounced so typing in the search box doesn't fire a request per keystroke.
    const t = setTimeout(() => void loadChats(query), query ? 250 : 0);
    return () => clearTimeout(t);
  }, [linked, query, loadChats]);

  /* ---------- thread ---------- */

  const openChat = useCallback(async (jid: string) => {
    setSelected(jid);
    setMessages([]);
    setParticipants([]);
    setUnread((u) => ({ ...u, [jid]: 0 }));

    try {
      const res = await api.getMessages(jid, { limit: PAGE, offset: 0 });
      // The API returns newest-first; the thread renders oldest-first.
      setMessages([...res.data].reverse());
      setTotal(res.pagination?.total ?? res.data.length);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load messages");
    }

    if (isGroup(jid)) {
      try {
        const info = await api.groupInfo(jid);
        setParticipants(info?.Participants ?? info?.participants ?? []);
      } catch {
        // Non-fatal: mentions fall back to raw phone numbers.
      }
    }
  }, []);

  const loadMore = useCallback(async () => {
    if (!selected || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await api.getMessages(selected, {
        limit: PAGE,
        offset: messages.length,
      });
      setMessages((prev) => [...[...res.data].reverse(), ...prev]);
      setTotal(res.pagination?.total ?? total);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load history");
    } finally {
      setLoadingMore(false);
    }
  }, [selected, messages.length, loadingMore, total]);

  /* ---------- live events ---------- */

  useEffect(() => {
    if (!linked) return;
    const es = new EventSource("/api/events");

    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);

    es.onmessage = (ev) => {
      let data: LiveEvent;
      try {
        data = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (data.event !== "message" || !data.message) return;

      const msg = data.message;
      const open = selectedRef.current;

      if (msg.chat_jid === open) {
        setMessages((prev) =>
          prev.some((m) => m.id === msg.id) ? prev : [...prev, msg],
        );
      } else if (!msg.is_from_me) {
        setUnread((u) => ({ ...u, [msg.chat_jid]: (u[msg.chat_jid] ?? 0) + 1 }));
      }

      // Float the chat to the top of the list, adding it if it's brand new.
      setChats((prev) => {
        const found = prev.find((c) => c.jid === msg.chat_jid);
        if (!found) {
          return [
            {
              jid: msg.chat_jid,
              name: msg.pushname || phoneOf(msg.chat_jid),
              last_message_time: msg.timestamp,
            },
            ...prev,
          ];
        }
        return prev.map((c) =>
          c.jid === msg.chat_jid ? { ...c, last_message_time: msg.timestamp } : c,
        );
      });
    };

    return () => es.close();
  }, [linked]);

  /* ---------- send ---------- */

  const send = useCallback(
    async (text: string, mentions: string[]) => {
      if (!selected) return;
      setSending(true);
      setError(null);

      const optimistic: ChatMessage = {
        id: `pending-${Date.now()}`,
        chat_jid: selected,
        sender_jid: status?.jid ?? "",
        content: text,
        timestamp: new Date().toISOString(),
        is_from_me: true,
      };
      setMessages((prev) => [...prev, optimistic]);

      try {
        const res = await api.sendMessage({
          phone: selected,
          message: text,
          ...(mentions.length ? { mentions } : {}),
        });
        setMessages((prev) =>
          prev.map((m) =>
            m.id === optimistic.id ? { ...m, id: res.message_id ?? m.id } : m,
          ),
        );
      } catch (e) {
        setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
        setError(e instanceof Error ? e.message : "Send failed");
      } finally {
        setSending(false);
      }
    },
    [selected, status?.jid],
  );

  /* ---------- render ---------- */

  const selectedChat = useMemo(
    () => chats.find((c) => c.jid === selected),
    [chats, selected],
  );

  if (engineDown) {
    return (
      <div className="grid h-full place-items-center p-6">
        <div className="max-w-md text-center">
          <h1 className="text-lg font-semibold">Engine not running</h1>
          <p className="mt-2 text-sm text-[var(--text-dim)]">
            Start the WhatsApp engine, then this page will connect automatically:
          </p>
          <pre className="mt-4 rounded-lg bg-[var(--bg-sunken)] px-4 py-3 text-left text-xs">
            ./engine/start.sh
          </pre>
        </div>
      </div>
    );
  }

  if (status && !linked) {
    return <LoginPanel onLinked={() => void refreshStatus()} />;
  }

  if (!status) {
    return (
      <div className="grid h-full place-items-center text-sm text-[var(--text-dim)]">
        Connecting to engine…
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-[var(--border)] bg-[var(--bg-raised)] px-4 py-2.5">
        <div className="flex items-baseline gap-3">
          <span className="text-sm font-semibold">Penny</span>
          <span className="text-xs text-[var(--text-dim)]">
            {phoneOf(status.jid) || "linked"}
          </span>
        </div>
        <div className="flex items-center gap-4 text-xs text-[var(--text-dim)]">
          <span className="flex items-center gap-1.5">
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                live ? "bg-[var(--accent)]" : "bg-[var(--text-dim)]"
              }`}
            />
            {live ? "live" : "reconnecting"}
          </span>
          <button
            onClick={async () => {
              await api.logout().catch(() => {});
              void refreshStatus();
            }}
            className="transition-colors hover:text-[var(--text)]"
          >
            Unlink
          </button>
        </div>
      </header>

      {error && (
        <div className="flex items-center justify-between border-b border-red-500/30 bg-red-500/10 px-4 py-2 text-xs text-red-500">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="ml-4 shrink-0">
            Dismiss
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <ChatList
          chats={chats}
          selected={selected}
          onSelect={(jid) => void openChat(jid)}
          query={query}
          onQuery={setQuery}
          unread={unread}
          loading={chatsLoading}
        />

        <main className="flex min-w-0 flex-1 flex-col bg-[var(--bg)]">
          {!selected ? (
            <div className="grid flex-1 place-items-center text-sm text-[var(--text-dim)]">
              Select a chat to view its history.
            </div>
          ) : (
            <>
              <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold">
                    {selectedChat?.name || phoneOf(selected)}
                  </div>
                  <div className="truncate text-xs text-[var(--text-dim)]">
                    {isGroup(selected)
                      ? `Group · ${participants.length || "?"} participants`
                      : phoneOf(selected)}
                  </div>
                </div>
                <div className="shrink-0 text-xs text-[var(--text-dim)]">
                  {messages.length} of {total} loaded
                </div>
              </div>

              <Thread
                jid={selected}
                messages={messages}
                participants={participants}
                hasMore={messages.length < total}
                loadingMore={loadingMore}
                onLoadMore={() => void loadMore()}
              />

              <Composer
                key={selected}
                isGroupChat={isGroup(selected)}
                participants={participants}
                onSend={send}
                sending={sending}
              />
            </>
          )}
        </main>
      </div>
    </div>
  );
}
