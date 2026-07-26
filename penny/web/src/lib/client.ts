"use client";

import type {
  Chat,
  ChatMessage,
  ConnectionStatus,
  Group,
  Pagination,
} from "./types";

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/gowa${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    throw new Error(body?.error ?? `Request failed (${res.status})`);
  }
  return body as T;
}

type Envelope<R> = { code: string; message: string; results: R };

export async function getStatus(): Promise<ConnectionStatus> {
  const r = await call<Envelope<ConnectionStatus>>("/app/status");
  return r.results;
}

/** Returns a QR string/URL the user scans to link the account. */
export async function getLoginQr(): Promise<{ qr_link?: string; qr_duration?: number }> {
  const r = await call<Envelope<{ qr_link?: string; qr_duration?: number }>>("/app/login");
  return r.results ?? {};
}

export async function logout(): Promise<void> {
  await call("/app/logout");
}

export async function listChats(params: {
  limit?: number;
  offset?: number;
  search?: string;
}): Promise<{ data: Chat[]; pagination: Pagination }> {
  const q = new URLSearchParams();
  q.set("limit", String(params.limit ?? 100));
  q.set("offset", String(params.offset ?? 0));
  if (params.search) q.set("search", params.search);
  const r = await call<Envelope<{ data: Chat[]; pagination: Pagination }>>(
    `/chats?${q}`,
  );
  return { data: r.results?.data ?? [], pagination: r.results?.pagination as Pagination };
}

export async function getMessages(
  jid: string,
  params: { limit?: number; offset?: number; search?: string } = {},
): Promise<{ data: ChatMessage[]; pagination: Pagination; chat_info?: Chat }> {
  const q = new URLSearchParams();
  q.set("limit", String(params.limit ?? 50));
  q.set("offset", String(params.offset ?? 0));
  if (params.search) q.set("search", params.search);
  const r = await call<
    Envelope<{ data: ChatMessage[]; pagination: Pagination; chat_info?: Chat }>
  >(`/chat/${encodeURIComponent(jid)}/messages?${q}`);
  return {
    data: r.results?.data ?? [],
    pagination: r.results?.pagination as Pagination,
    chat_info: r.results?.chat_info,
  };
}

export async function sendMessage(args: {
  phone: string;
  message: string;
  mentions?: string[];
  reply_message_id?: string;
}): Promise<{ message_id?: string }> {
  const r = await call<Envelope<{ message_id?: string }>>("/send/message", {
    method: "POST",
    body: JSON.stringify(args),
  });
  return r.results ?? {};
}

export async function myGroups(): Promise<Group[]> {
  const r = await call<Envelope<{ data?: Group[] } | Group[]>>("/user/my/groups");
  const results = r.results as { data?: Group[] } | Group[] | undefined;
  if (Array.isArray(results)) return results;
  return results?.data ?? [];
}

export async function groupInfo(jid: string): Promise<Group | undefined> {
  const q = new URLSearchParams({ group_id: jid });
  const r = await call<Envelope<Group>>(`/group/info?${q}`);
  return r.results;
}
