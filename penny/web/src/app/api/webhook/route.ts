import { NextRequest, NextResponse } from "next/server";
import crypto from "node:crypto";
import { publish } from "@/lib/bus";
import type { ChatMessage, LiveEvent } from "@/lib/types";

export const dynamic = "force-dynamic";

const SECRET = process.env.WEBHOOK_SECRET ?? "secret";

function signatureValid(raw: string, header: string | null): boolean {
  if (!header) return false;
  const expected = crypto.createHmac("sha256", SECRET).update(raw, "utf8").digest("hex");
  const received = header.replace(/^sha256=/, "");
  const a = Buffer.from(expected, "hex");
  const b = Buffer.from(received, "hex");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

type WebhookBody = {
  event?: string;
  device_id?: string;
  payload?: {
    id?: string;
    chat_id?: string;
    from?: string;
    from_name?: string;
    timestamp?: string;
    is_from_me?: boolean;
    body?: string;
    caption?: string;
    [k: string]: unknown;
  };
};

/** Turns a `message` webhook payload into the shape the chat-messages endpoint returns. */
function toMessage(p: NonNullable<WebhookBody["payload"]>): ChatMessage | undefined {
  if (!p.chat_id || !p.id) return undefined;
  return {
    id: p.id,
    chat_jid: p.chat_id,
    sender_jid: p.from ?? "",
    content: p.body ?? p.caption ?? "",
    timestamp: p.timestamp ?? new Date().toISOString(),
    is_from_me: Boolean(p.is_from_me),
    media_type: (p.media_type as string | undefined) ?? null,
    pushname: p.from_name,
  };
}

export async function POST(req: NextRequest) {
  const raw = await req.text();

  if (!signatureValid(raw, req.headers.get("x-hub-signature-256"))) {
    return NextResponse.json({ error: "bad signature" }, { status: 401 });
  }

  let body: WebhookBody;
  try {
    body = JSON.parse(raw);
  } catch {
    return NextResponse.json({ error: "bad json" }, { status: 400 });
  }

  const payload = body.payload ?? {};
  const evt: LiveEvent = {
    event: body.event ?? "unknown",
    chat_jid: payload.chat_id,
    message: body.event === "message" ? toMessage(payload) : undefined,
    raw: body,
  };

  publish(evt);
  return NextResponse.json({ ok: true });
}
