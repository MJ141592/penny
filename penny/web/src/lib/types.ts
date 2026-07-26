export type Chat = {
  jid: string;
  name: string;
  last_message_time: string;
  ephemeral_expiration?: number;
  created_at?: string;
  updated_at?: string;
  archived?: boolean;
};

export type Reaction = {
  emoji?: string;
  sender_jid?: string;
};

export type ChatMessage = {
  id: string;
  chat_jid: string;
  sender_jid: string;
  content: string;
  timestamp: string;
  is_from_me: boolean;
  media_type?: string | null;
  filename?: string;
  reactions?: Reaction[];
  pushname?: string;
};

export type Pagination = { limit: number; offset: number; total: number };

export type GroupParticipant = {
  JID?: string;
  jid?: string;
  PhoneNumber?: string;
  DisplayName?: string;
  IsAdmin?: boolean;
  IsSuperAdmin?: boolean;
};

export type Group = {
  JID?: string;
  jid?: string;
  Name?: string;
  name?: string;
  Participants?: GroupParticipant[];
  participants?: GroupParticipant[];
};

export type ConnectionStatus = {
  is_connected: boolean;
  is_logged_in: boolean;
  device_id?: string;
  jid?: string;
};

/** A normalized inbound event pushed from the webhook to the browser over SSE. */
export type LiveEvent = {
  event: string;
  chat_jid?: string;
  message?: ChatMessage;
  raw?: unknown;
};

export const isGroup = (jid: string) => jid.endsWith("@g.us");

/** Strips the WhatsApp server suffix and device part: "628123:12@s.whatsapp.net" -> "628123". */
export function phoneOf(jid: string | undefined): string {
  if (!jid) return "";
  return jid.split("@")[0].split(":")[0];
}
