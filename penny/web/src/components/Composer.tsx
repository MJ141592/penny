"use client";

import { useMemo, useRef, useState } from "react";
import type { GroupParticipant } from "@/lib/types";
import { phoneOf } from "@/lib/types";

type Candidate = { phone: string; name: string };

/**
 * The parent mounts this with `key={jid}`, so switching chats remounts it and
 * drafts never leak between conversations.
 */
export default function Composer({
  isGroupChat,
  participants,
  onSend,
  sending,
}: {
  isGroupChat: boolean;
  participants: GroupParticipant[];
  onSend: (text: string, mentions: string[]) => Promise<void>;
  sending: boolean;
}) {
  const [text, setText] = useState("");
  const [ghostEveryone, setGhostEveryone] = useState(false);
  const [picker, setPicker] = useState<{ open: boolean; query: string; at: number }>({
    open: false,
    query: "",
    at: -1,
  });
  const [highlight, setHighlight] = useState(0);
  const ref = useRef<HTMLTextAreaElement>(null);

  const candidates: Candidate[] = useMemo(() => {
    const list = participants
      .map((p) => {
        const phone = phoneOf(p.JID ?? p.jid ?? p.PhoneNumber);
        return { phone, name: p.DisplayName || phone };
      })
      .filter((c) => c.phone);

    const q = picker.query.toLowerCase();
    if (!q) return list.slice(0, 8);
    return list
      .filter(
        (c) => c.name.toLowerCase().includes(q) || c.phone.includes(q),
      )
      .slice(0, 8);
  }, [participants, picker.query]);

  /** Detects an in-progress "@word" immediately before the caret. */
  function syncPicker(value: string, caret: number) {
    if (!isGroupChat) return;
    const upto = value.slice(0, caret);
    const match = /(?:^|\s)@([\p{L}\p{N}_.\-]*)$/u.exec(upto);
    if (match) {
      setPicker({ open: true, query: match[1], at: caret - match[1].length - 1 });
      setHighlight(0);
    } else {
      setPicker({ open: false, query: "", at: -1 });
    }
  }

  function choose(c: Candidate) {
    const before = text.slice(0, picker.at);
    const after = text.slice(picker.at + picker.query.length + 1);
    // WhatsApp mentions are encoded as @<phone> in the body; GOWA parses these
    // out and attaches the proper mention metadata on send.
    const next = `${before}@${c.phone} ${after}`;
    setText(next);
    setPicker({ open: false, query: "", at: -1 });
    requestAnimationFrame(() => {
      const pos = before.length + c.phone.length + 2;
      ref.current?.focus();
      ref.current?.setSelectionRange(pos, pos);
    });
  }

  async function submit() {
    const body = text.trim();
    if (!body || sending) return;
    const mentions = ghostEveryone ? ["@everyone"] : [];
    await onSend(body, mentions);
    setText("");
    setGhostEveryone(false);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (picker.open && candidates.length > 0) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlight((h) => (h + 1) % candidates.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight((h) => (h - 1 + candidates.length) % candidates.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        choose(candidates[highlight]);
        return;
      }
      if (e.key === "Escape") {
        setPicker({ open: false, query: "", at: -1 });
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  }

  return (
    <div className="relative border-t border-[var(--border)] bg-[var(--bg-raised)] p-3">
      {picker.open && candidates.length > 0 && (
        <ul className="absolute bottom-full left-3 mb-2 w-72 overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--bg-raised)] shadow-lg">
          {candidates.map((c, i) => (
            <li key={c.phone}>
              <button
                onMouseDown={(e) => {
                  e.preventDefault();
                  choose(c);
                }}
                onMouseEnter={() => setHighlight(i)}
                className={`flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm ${
                  i === highlight ? "bg-[var(--bg-sunken)]" : ""
                }`}
              >
                <span className="truncate">{c.name}</span>
                <span className="shrink-0 text-xs text-[var(--text-dim)]">
                  {c.phone}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {isGroupChat && (
        <label className="mb-2 flex w-fit cursor-pointer items-center gap-2 text-xs text-[var(--text-dim)]">
          <input
            type="checkbox"
            checked={ghostEveryone}
            onChange={(e) => setGhostEveryone(e.target.checked)}
            className="accent-[var(--accent)]"
          />
          Notify everyone (ghost mention — no <code>@</code> shown in the text)
        </label>
      )}

      <div className="flex items-end gap-2">
        <textarea
          ref={ref}
          value={text}
          rows={1}
          onChange={(e) => {
            setText(e.target.value);
            syncPicker(e.target.value, e.target.selectionStart ?? 0);
          }}
          onKeyDown={onKeyDown}
          placeholder={
            isGroupChat
              ? "Message the group — type @ to mention someone"
              : "Type a message"
          }
          className="max-h-40 min-h-[42px] flex-1 resize-y rounded-xl border border-[var(--border)] bg-[var(--bg)] px-3 py-2.5 text-sm outline-none placeholder:text-[var(--text-dim)] focus:border-[var(--accent)]"
        />
        <button
          onClick={() => void submit()}
          disabled={sending || !text.trim()}
          className="h-[42px] rounded-xl bg-[var(--accent)] px-5 text-sm font-medium text-[var(--accent-fg)] transition-opacity disabled:opacity-40"
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </div>
    </div>
  );
}
