# Penny — programmatic WhatsApp Business control

A chat console over a real WhatsApp Business account, built to prove three
capabilities before layering AI agents on top:

1. **Send and receive messages** — live, both directions
2. **Group chat** — group-wide messages and directed `@` mentions
3. **Full chat history** — every message in every chat and group you're in

## Architecture

```
  your phone ──(companion device link)── GOWA engine ──REST/webhook── Next.js app
                                          :3000                        :3001
```

- **`engine/`** — [GOWA](https://github.com/aldinokemal/go-whatsapp-web-multidevice),
  a Go server wrapping `whatsmeow`. Speaks the WhatsApp Web multi-device
  protocol, persists messages to SQLite, and posts inbound events to a webhook.
- **`web/`** — Next.js 16 app. Talks to GOWA only through a server-side proxy
  (`/api/gowa/*`), so the engine credentials never reach the browser.

Inbound flow: WhatsApp → GOWA → HMAC-signed webhook → in-memory bus → SSE → UI.

## Running it

```bash
npm install          # once
npm run dev          # starts both the engine and the web app
```

Then open <http://localhost:3001> and scan the QR with
**WhatsApp → Settings → Linked devices → Link a device**.

To run the halves separately:

```bash
npm run engine       # GOWA on :3000
npm run web          # Next.js on :3001
```

History syncs in over the first minute or two after linking — the chat list
fills in progressively, so give it a moment before judging coverage.

## Using it

- **Chat list** — every DM and group, newest first, searchable.
- **History** — open a chat and click *Load earlier messages* to page back.
  The header shows `N of M loaded` so you can see the true depth.
- **Send** — type and hit Enter (Shift+Enter for a newline).
- **Mention someone in a group** — type `@`, pick from the participant list.
  This inserts the mention WhatsApp actually notifies on.
- **Mention everyone** — tick *Notify everyone*. This is a ghost mention: all
  participants get pinged without `@number` cluttering the message text.

## Configuration

All settings live in `.env` at the repo root, read by both halves
(`web/.env.local` is a symlink to it).

| Variable | Purpose |
|---|---|
| `GOWA_PORT` / `GOWA_URL` | Where the engine listens |
| `GOWA_USER` / `GOWA_PASS` | Basic auth on the engine API |
| `WEB_PORT` | Where the UI listens |
| `WEBHOOK_URL` | Where GOWA posts inbound events |
| `WEBHOOK_SECRET` | HMAC key; unsigned or mis-signed webhooks are rejected 401 |

`.env` and `engine/storages/` are gitignored. **`engine/storages/` holds the
linked-device credentials** — anyone with that directory can act as your
WhatsApp account. Treat it like a password.

## Next step: agents

GOWA ships an MCP server exposing 50+ tools over this same session:

```bash
./engine/gowa mcp --port 8080
```

Point an agent at it and everything the UI can do becomes callable — no
additional integration work. That was the main reason for choosing this engine.

## Known limits

- `/user/my/groups` returns at most **500 groups**. This is a WhatsApp protocol
  limit on `GetJoinedGroups()`, not something the API layer can work around.
- History depth is whatever WhatsApp's history sync hands the companion device
  on link. It is deep but not guaranteed to be complete back to a chat's origin.
- The live event bus is in-memory: restarting the web app drops open SSE
  streams (they reconnect) and the recent-event buffer.
- Media messages are listed with their type and filename but not rendered
  inline; the engine downloads them under `engine/storages/`.

## The tradeoff you accepted

This uses the WhatsApp Web protocol, not Meta's official Business Platform.
That is what makes requirements 2 and 3 possible at all — the official Cloud API
has no chat history, and its Groups API caps groups at 8 participants and can
only message groups it created via invite link.

The cost is that this is unofficial and against WhatsApp's Terms of Service.
There is a real, if commonly tolerated, risk of the linked number being banned.
You chose to link the production business number knowing this.
