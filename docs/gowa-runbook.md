# GOWA runbook — pairing a WhatsApp account (M1b)

**This procedure needs a physical phone and cannot be automated.** It is the M1b spike: the go/no-go
for live ingest. Timebox: one day.

Deployment shape, service variables and platform gotchas live in
[`railway-deployment.md`](./railway-deployment.md). This file is the operator procedure only.

## Acceptance criterion

Stated exactly as in the plan:

> A logged webhook body with `chat_id` ending `@g.us` and a `from` that isn't the paired number —
> **and** the same after a forced redeploy.

Two halves. The first proves GOWA reads existing group chats at all. The second proves the whatsmeow
session survives a deploy, i.e. that the Postgres `DB_URI` decision works and re-pairing is not a
per-deploy event.

---

## Rule zero: never pair a primary WhatsApp account

Pair the **disposable spare business account** — a friend's spare, where everyone involved accepts it
may be banned.

[whatsmeow #810](https://github.com/tulir/whatsmeow/issues/810) tracks WhatsApp bans and
"account at risk" warnings for unofficial clients. It hits **low-volume, reply-only usage**, it is
not whatsmeow-specific (Baileys too), and it is **closed as not planned upstream** — there is no
mitigation and no fix coming. Penny's near-zero outbound volume reduces the risk; it does not remove
it.

The point is where the blast radius lands. A ban does not take out a container you can redeploy — it
takes out a *person's* WhatsApp account: chat history, groups, everything, permanently. Pairing a
disposable account is the entire mitigation, and it only works if it is never relaxed "just this
once" for convenience.

---

## Before you start

- [ ] `gowa` deployed from a **pinned** image tag (`:v9.0.0`, never `latest`).
- [ ] `DB_URI` set to the hand-composed `postgres://…?sslmode=disable` value — **not**
      `${{Postgres.DATABASE_URL}}`, which panics at startup. Confirm the service reached a running
      state before you touch a phone: `railway logs --service gowa`.
- [ ] Volume mounted at `/app/storages` for `chatstorage.db`.
- [ ] `APP_BASIC_AUTH=penny:<password>` set, with **no `:` and no `,` in the password** (either is a
      hard startup crash).
- [ ] `APP_UI_ENABLED=false`, `WHATSAPP_WEBHOOK_EVENTS=message`,
      `WHATSAPP_WEBHOOK_SECRET` set to something other than the upstream default `"secret"`.
- [ ] `WHATSAPP_WEBHOOK=http://penny.railway.internal:8000/api/whatsapp/webhook` and `PORT=8000` set
      as a service variable on `penny` (`${{svc.PORT}}` does not auto-resolve).
- [ ] A webhook receiver deployed on `penny` that returns **200 fast** and logs the raw body. For the
      spike this is a temporary endpoint (Track D); M6 replaces it with the real HMAC-verifying,
      idempotent route.
- [ ] The target phone in hand, plus a **second** phone or person who can post in the test group.
- [ ] A throwaway WhatsApp group with invented content — nothing real.

**Logging exception, spike only.** The standing rule is *never log message text*. Capturing a full
webhook body is the deliverable of this spike, so it is a deliberate one-off: use the throwaway group
with invented messages, and delete the captured logs once the payload shape is written down.

---

## Procedure

### 1. Temporarily attach a public domain to `gowa`

```sh
railway domain --service gowa
```

The pairing endpoints are only reachable over the public internet — `railway run` executes on your
machine and cannot reach `*.railway.internal`, and you cannot scan a QR from inside `railway ssh`.

Note that `/health` and `/statics` sit **outside** GOWA's basic auth, so while this domain exists the
QR PNG is fetchable by anyone with the URL. The filename is a UUID and the file is deleted after
~30s, which is why this window is measured in minutes.

### 2. Create a device, THEN request the QR

**v9 needs a device to exist before it will issue a QR.** This step does not appear in the
v8-era documentation these notes were derived from, and without it `/app/login` answers
`400 DEVICE_ID_REQUIRED` — which reads like a credentials or config fault and is neither.
Confirmed by doing it on 2026-07-25.

```sh
# 1. Create the device. `name` is required; an empty body is a 400.
curl -u penny:<password> -X POST https://<gowa-domain>/devices \
     -H 'Content-Type: application/json' -d '{"name":"penny"}'
# -> {"results":{"id":"<device_id>","state":"disconnected","jid":"", ...}}

# 2. Now the QR, scoped to that device.
curl -u penny:<password> "https://<gowa-domain>/app/login?device_id=<device_id>"
```

Returns `{device_id, qr_link, qr_duration}`.

`qr_duration` came back as **30 seconds**, not the minutes the v8 notes implied. Have the phone
unlocked and on the Linked Devices screen *before* you request it. This one-liner fetches a fresh
code and opens it, so re-running is cheap:

```sh
curl -s -u penny:<password> "https://<gowa-domain>/app/login?device_id=<device_id>" \
| python3 -c "import json,sys,subprocess; u=json.load(sys.stdin)['results']['qr_link']; print(u); subprocess.run(['open',u])"
```

- **`qr_link` is a PNG URL, not a raw QR string.** Open it in a browser; don't try to render it as
  text.
- **The endpoint blocks until the first QR is produced, with a 120s timeout.** A slow response is
  expected, not a hang.
- `qr_duration` is your scan window. If it lapses, call `/app/login` again for a fresh code.

### 3. Scan with the target phone

On the phone: WhatsApp → Linked devices → Link a device → scan the PNG.

**Pairing-code alternative** (no camera, or the phone can't see your screen):

```sh
curl -u penny:<password> "https://<gowa-domain>/app/login-with-code?phone=628xxxxxxxx"
```

`phone` validates as `^\+?[0-9]{1,15}$`. Enter the returned code on the phone under Linked devices →
Link with phone number instead.

**Passkey path**, if the account is passkey-flagged:

1. `GET /app/passkey` → `{status, challenge, code}`.
2. Complete the WebAuthn assertion in **desktop Chrome on `web.whatsapp.com`**.
3. `POST /app/passkey/response`, then `POST /app/passkey/confirm`.

### 4. Confirm the pairing

> **v9 requires a device id on every `/app/*` route.** Verified against the deployed
> `v9.0.0` image on 2026-07-25 — this is a breaking change from the v8-era docs these notes
> were written from. Without it you get, on *every* `/app/*` path including `/app/status`,
> `/app/devices` and `/app/reconnect`:
>
> ```json
> {"code":"DEVICE_ID_REQUIRED","message":"device_id is required via X-Device-Id header or device_id query"}
> ```
>
> The status is **400, not 401**, so it is easy to misread as a credentials problem when it is
> not. Before the first pairing there is no id to send, which makes `/app/status` unreachable
> by construction. Use **`GET /devices`** — the one device route that needs no id — to ask
> whether anything is paired at all. It returns `{"code":"SUCCESS","results":null}` when
> nothing is (`null`, not `[]`).

```sh
# Is anything paired? Works before the first pairing.
curl -u penny:<password> https://<gowa-domain>/devices

# Once a device exists, its live state:
curl -u penny:<password> "https://<gowa-domain>/app/status?device_id=<device_id>"
```

Returns `{is_connected, is_logged_in, device_id, jid}`. Both booleans must be true.
`app/gowa.py`'s `get_status()` does exactly these two hops, which is why it can tell
"sidecar is down" apart from "sidecar is up, nothing paired yet".

**Write down `jid`.** It is the paired number, and the acceptance criterion is defined against it:
the captured webhook's `from` must *not* be this value.

### 5. Delete the public domain — do this now, not later

Remove the domain in the `gowa` service's Settings → Networking. (The CLI verb for *removing* a
domain is unverified here; the dashboard definitely works.)

Everything from here on runs over private networking. Confirm the domain is gone before moving on —
leaving it attached is what turns gotcha 8 from a two-minute exposure into a standing one.

### 6. Add the paired number to a throwaway group

Create a group on a *different* phone, invented content only, and add the paired number to it. The
paired account inherits group membership as a companion device, so no migration or invite dance is
needed.

### 7. Post from a DIFFERENT participant

Send a message into the group from a phone that is **not** the paired account.

This matters: messages you send *from* the paired account are also delivered to the webhook, with
`is_from_me: true`. Confirming with your own message proves nothing — the whole question is whether
*other participants'* group messages arrive.

### 8. Capture the webhook body

```sh
railway logs --service penny
```

You are looking for, and should save verbatim (redacting real numbers):

```json
{
  "event": "message",
  "device_id": "628123456789@s.whatsapp.net",
  "session_id": "penny",
  "payload": {
    "id": "3EB0C127D7BACC83D6A1",
    "timestamp": "2026-07-25T10:30:00Z",
    "is_from_me": false,
    "chat_id": "120363402106XXXXX@g.us",
    "from": "628987654321@s.whatsapp.net",
    "from_lid": "251556368777322@lid",
    "from_name": "John Doe",
    "body": "hello everyone"
  }
}
```

Check, explicitly:

- `payload.chat_id` ends in `@g.us` (there is no `is_group` field — this suffix is the only group
  signal).
- `payload.from` is **not** the `jid` from step 4.
- `payload.is_from_me` is `false`.
- Whether `from_lid` is present, and whether `from` is a `@s.whatsapp.net` or a `@lid`. Both fields
  feed `members.wa_jid` / `members.wa_lid`; keying on one alone fragments a person in two.
- Whether `from_name` is present at all (it is omitted entirely when empty).

Record `chat_id` — that is the `whatsapp_links.group_external_id` value M6 needs.

### 9. Force a redeploy and prove the session survived

```sh
railway redeploy --service gowa --yes
```

Expect downtime: `gowa` has a volume, and Railway prevents two deployments being mounted to the same
service at once, so there is no zero-downtime path here. Wait for the new deployment to go live, then:

1. `GET /app/status` over private networking (`railway ssh --service gowa`, then curl `localhost:3000`
   — the public domain is gone by now) → `is_logged_in` still true, same `jid`, **no QR prompted**.
2. Post another message from the other participant.
3. Confirm a second webhook body arrives.

If step 9 requires a re-pair, the session is not actually in Postgres — check `DB_URI` for the
`postgresql://` prefix panic and for a missing `?sslmode=disable`, and check Postgres for
`whatsmeow_*` tables.

---

## While you're in there: close the plan's open questions

M1b is the designated place to answer these empirically rather than up front.

**Open question #1 — is GOWA's chat storage Postgres-capable or SQLite-pinned?** The session store
definitely supports Postgres; that's the one that matters. For chat storage, check both sides:

```sh
railway ssh --service gowa        # ls -la /app/storages — is chatstorage.db there?
railway volume browse /
```

and list the tables in the Postgres database. Expected result, per the source notes: `whatsmeow_*`
session tables in Postgres, `chatstorage.db` on the volume. If the file lands somewhere other than
`/app/storages`, move the mount path — that path is an assumption, not a verified fact.

**Open question #2 — history-sync depth.** Note what arrives at pair time: how far back, how many
messages, from which chats. GOWA's history sync is opportunistic and server-controlled. The plan
assumes forward-only, and the `.txt` importer stays regardless — this measurement just tells you how
wrong "forward-only" is.

---

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Service panics at startup, Postgres in the trace | `DB_URI` starts `postgresql://` | Must start `postgres:` — compose it from `${{Postgres.PG*}}`, never `${{Postgres.DATABASE_URL}}` |
| Startup fails with an SSL/TLS error | lib/pq defaults to `sslmode=require`; Railway's internal Postgres doesn't terminate TLS | Append `?sslmode=disable` |
| `Fatalln` at startup, basic-auth parsing | `:` (or `,`) in the `APP_BASIC_AUTH` password | Regenerate the password without them |
| "too many colons in address" | `APP_HOST=::` | Use `APP_HOST=[::]` — bracketed |
| Crash-loop, `exec: "rest"` | Start command overrides the image `ENTRYPOINT` | Leave the start command blank, or use `/entrypoint.sh rest` |
| `/app/login` returns nothing useful, QR never generates, "client outdated (405)" | WhatsApp rejected a stale hardcoded client version | Only fixable by upgrading the pinned tag — bump and redeploy |
| Webhook never fires | Private-networking wiring | `railway logs --network --peer gowa --port 3000`; check `PORT=8000` is set on `penny` and that the webhook URL has a port in it |
| WhatsApp websocket won't connect | Unverified GitHub account → "Limited Trial" restricts outbound network access and ports | Verify at railway.com/verify, move to Hobby |
| Webhook fires but every body has `is_from_me: true` | You're testing with the paired phone | Post from a different participant |

---

## After the spike

- Save the redacted webhook body — it is the fixture M6's ingestion tests replay.
- Delete the captured raw logs (see the logging exception above).
- Confirm the public domain on `gowa` is still gone.
- Session health is a product concern, not just an ops one: the plan treats re-pair as a **routine
  event**. M6 monitors `GET /app/status` and surfaces a re-pair QR flow in the UI — which means
  repeating steps 1–5 with a public domain, so keep this runbook.
