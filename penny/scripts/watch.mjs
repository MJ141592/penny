#!/usr/bin/env node
/**
 * Streams WhatsApp events to the shell.
 *
 * Subscribes to the app's SSE endpoint (fed by GOWA's webhook) and pretty-prints
 * every event as it lands. This is the semantic layer: messages, reactions,
 * receipts, presence. For raw protocol nodes, run the engine with DEBUG=true.
 *
 *   node scripts/watch.mjs            # messages only
 *   node scripts/watch.mjs --all      # every event type
 *   node scripts/watch.mjs --json     # raw payloads, one JSON object per line
 */

const args = new Set(process.argv.slice(2));
const SHOW_ALL = args.has("--all");
const AS_JSON = args.has("--json");
const URL_ = process.env.WATCH_URL ?? "http://localhost:3001/api/events";

const C = {
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  blue: "\x1b[34m",
  magenta: "\x1b[35m",
  cyan: "\x1b[36m",
};

const clock = () => new Date().toTimeString().slice(0, 8);
const jid = (s = "") => s.split("@")[0].split(":")[0];
const isGroup = (s = "") => s.endsWith("@g.us");

const EVENT_COLOR = {
  message: C.green,
  "message.ack": C.dim,
  "message.reaction": C.magenta,
  "message.revoked": C.red,
  "message.edited": C.yellow,
  "group.participants": C.blue,
  "call.offer": C.red,
  chat_presence: C.dim,
};

function render(evt) {
  if (AS_JSON) {
    process.stdout.write(JSON.stringify(evt) + "\n");
    return;
  }

  const name = evt.event ?? "unknown";
  if (name === "connected") {
    console.log(`${C.dim}${clock()}${C.reset} ${C.cyan}◆ connected${C.reset} ${C.dim}${URL_}${C.reset}`);
    return;
  }
  if (!SHOW_ALL && name !== "message") return;

  const color = EVENT_COLOR[name] ?? C.reset;
  const p = evt.raw?.payload ?? {};

  if (name === "message" && evt.message) {
    const m = evt.message;
    const dir = m.is_from_me ? `${C.cyan}▶ out${C.reset}` : `${C.green}◀ in ${C.reset}`;
    const where = isGroup(m.chat_jid)
      ? `${C.blue}group:${jid(m.chat_jid)}${C.reset}`
      : `${C.dim}dm:${jid(m.chat_jid)}${C.reset}`;
    const who = m.pushname || jid(m.sender_jid) || "?";
    const media = m.media_type ? ` ${C.yellow}[${m.media_type}]${C.reset}` : "";

    console.log(
      `${C.dim}${clock()}${C.reset} ${dir} ${where} ${C.bold}${who}${C.reset}${media}`,
    );
    if (m.content) {
      for (const line of m.content.split("\n")) {
        console.log(`         ${line}`);
      }
    }
    return;
  }

  // Everything else: one compact line with whatever identifying fields exist.
  const bits = [p.chat_id && `chat=${jid(p.chat_id)}`, p.from && `from=${jid(p.from)}`, p.id && `id=${p.id}`]
    .filter(Boolean)
    .join(" ");
  console.log(`${C.dim}${clock()}${C.reset} ${color}● ${name}${C.reset} ${C.dim}${bits}${C.reset}`);
}

async function connect() {
  console.log(
    `${C.dim}watching ${URL_} — ${SHOW_ALL ? "all events" : "messages only (--all for everything)"}${C.reset}`,
  );

  for (;;) {
    try {
      const res = await fetch(URL_, { headers: { Accept: "text/event-stream" } });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line.
        let split;
        while ((split = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, split);
          buf = buf.slice(split + 2);
          for (const line of frame.split("\n")) {
            if (!line.startsWith("data: ")) continue;
            try {
              render(JSON.parse(line.slice(6)));
            } catch {
              /* keepalive or partial frame */
            }
          }
        }
      }
      throw new Error("stream ended");
    } catch (err) {
      console.log(
        `${C.dim}${clock()}${C.reset} ${C.red}✕ ${err.message}${C.reset} ${C.dim}— retrying in 3s${C.reset}`,
      );
      await new Promise((r) => setTimeout(r, 3000));
    }
  }
}

connect();
