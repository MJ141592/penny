# Penny HTTP API contract

**This document is the source of truth until the backend exists.** M3 builds the frontend against
hand-authored fixtures that conform to it; M4 builds the backend to satisfy it. When both exist,
`openapi-typescript` generates `frontend/src/api/types.ts` from `/openapi.json` and any divergence
from this file is a bug in one of them.

---

## Conventions

| | |
|---|---|
| Base path | `/api`. The SPA is served from the same origin, so there is no CORS in production and no CSRF token (`SameSite=Lax` + same-origin covers it). |
| Content type | `application/json` in and out, except the two `multipart/form-data` import routes. |
| Timestamps | ISO 8601, **always UTC with a trailing `Z`** — `"2026-07-17T09:30:00Z"`. Never a local offset. The client renders in `household.timezone` with `Intl`. |
| Ids | UUID v4 strings. |
| Money | Decimal **strings**, never floats — `"19.42"`. |
| Auth | HttpOnly signed cookie `penny_session`, sent automatically (same origin). Every route below needs it except `/api/auth/login`, `/api/health`, `/api/ai/status`, `/api/whatsapp/webhook` and `/api/internal/tick`. |
| Trailing slashes | Never. `/api/feed`, not `/api/feed/`. |

### Error envelope

**Every** non-2xx response body is exactly:

```json
{ "detail": "Human-readable sentence, safe to render directly." }
```

`detail` is **always a string**. FastAPI's default `RequestValidationError` handler returns a *list*
of error objects; the app installs a handler that flattens it to one sentence, so the client can do
`toast(body.detail)` unconditionally and never has to type-narrow.

### 404, never 403

Any resource belonging to another household returns **404 with the same body as a genuinely missing
id**. There is no 403 anywhere in this API for cross-tenant access, because a 403 is an existence
oracle: it confirms the row exists somewhere. `GET /api/events/{other-household-event-id}` and
`GET /api/events/{random-uuid}` are byte-identical responses.

401 means "you are not signed in" and is the client's cue to redirect to `/login`.

### Status codes

| Code | Meaning |
|---|---|
| 200 | Body returned. |
| 202 | Accepted; work continues in the background. Poll the resource. |
| 204 | Success, no body. Do not parse the response. |
| 400 | Malformed request the schema can't express (e.g. a `chat_id` that isn't a group). |
| 401 | Not signed in, or bad credentials, or bad webhook signature. |
| 404 | Missing **or** not yours. |
| 409 | Conflict (duplicate import, group already linked). |
| 413 | Upload over the size cap. |
| 422 | Schema validation failed. |
| 429 | Rate limited (login attempts, report generation). |
| 5xx | Bug. The only class the client may retry. |

### react-query rules

1. **Never retry a 4xx.** A 401 retried three times just delays the redirect; a 422 retried is a
   guaranteed-identical failure.
   ```ts
   retry: (failureCount, error) =>
     failureCount < 2 && error instanceof ApiError && error.status >= 500,
   ```
2. `queryClient.clear()` **on logout** — otherwise the next person on a shared laptop sees the
   previous family's cached health feed.
3. Feed and upcoming use `refetchInterval: 60_000`. Import status polls at `2_000` until the status
   is terminal, then stops.

---

## The `Event` model

The feed is one list of four kinds. Common fields sit at the top level; everything kind-specific
lives under `details`, discriminated by `kind`.

### Common base

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | |
| `kind` | `"symptom" \| "appointment" \| "medication" \| "note"` | The discriminant. |
| `occurred_at` | `string` | ISO UTC. **Never null** — falls back to the earliest source message's `sent_at`, because Postgres `DESC` sorts `NULLS FIRST` and undated events would otherwise pin to the top of the feed. |
| `occurred_at_precision` | `"exact" \| "day" \| "week" \| "month" \| "unknown"` | Drives rendering: `exact` shows a time, `day` shows a date, `week`/`month` show "week of…"/"in July". |
| `title` | `string` | ≤80 chars, no trailing period. |
| `body` | `string \| null` | ≤400 chars, third person, factual. |
| `actor` | `{ member_id: string, display_name: string } \| null` | Who did or reported it. `null` when unattributable. |
| `source_excerpts` | `SourceExcerpt[]` | Verbatim evidence. Powers `SourceDisclosure`. May be empty for a human-authored event. |
| `edited_at` | `string \| null` | Non-null means a human edited it, and **re-extraction will never overwrite it again**. Show an "edited" marker. |
| `created_at` | `string` | |

`SourceExcerpt`:

```json
{
  "message_id": "6f1f9d5e-1b3a-4a2b-9d61-2f0a8c1e77aa",
  "sent_at": "2026-07-17T08:12:00Z",
  "sender": "Sarah",
  "quote": "Just got back from the GP with Mum, bloods taken, review in 2 weeks",
  "transcribed": false
}
```

`transcribed` means those words were **spoken into a voice note and written down by a speech
model**, not typed. Always present; `false` for a typed message and for every event extracted
before transcription existed. The UI must say so — speech-to-text mishears names, medicines and
doses, which is precisely the class of error a family can catch and nobody else can.

### `details`, per kind

| `kind` | `details` |
|---|---|
| `symptom` | `{ symptom: string, severity: "mild"\|"moderate"\|"severe"\|"unknown", body_site: string\|null, duration_text: string\|null }` |
| `appointment` | `{ appointment_kind: "gp"\|"specialist"\|"hospital"\|"test"\|"therapy"\|"other", provider_name: string\|null, location: string\|null, attendees: string[], outcome: string\|null, follow_up_actions: string[], status: "scheduled"\|"attended"\|"cancelled"\|"missed" }` |
| `medication` | `{ medication_name: string, dose_text: string\|null, action: "started"\|"stopped"\|"changed"\|"missed"\|"refilled"\|"side_effect"\|"other", prescriber: string\|null }` |
| `note` | `{ category: "logistics"\|"mood"\|"finance"\|"equipment"\|"admin"\|"other" }` |

An appointment is **one event for its whole life**. It is created when the family mentions
scheduling it and *gains* `details.outcome`, `details.follow_up_actions` and
`status: "attended"` when they discuss it afterwards — merged onto the same row via `dedup_key`.
There is no separate "appointment outcome" event and no parent/child link.

### TypeScript

```ts
type EventKind = "symptom" | "appointment" | "medication" | "note";

interface EventBase {
  id: string;
  occurred_at: string;
  occurred_at_precision: "exact" | "day" | "week" | "month" | "unknown";
  title: string;
  body: string | null;
  actor: { member_id: string; display_name: string } | null;
  source_excerpts: SourceExcerpt[];
  edited_at: string | null;
  created_at: string;
}

type Event =
  | (EventBase & { kind: "symptom"; details: SymptomDetails })
  | (EventBase & { kind: "appointment"; details: AppointmentDetails })
  | (EventBase & { kind: "medication"; details: MedicationDetails })
  | (EventBase & { kind: "note"; details: NoteDetails });
```

`switch (event.kind)` is then exhaustive, and a new kind fails `tsc -b` at every render site —
which is the entire point of shaping the API response this way.

### The LLM emits ONE FLAT model; the API converts

**OpenAI strict structured outputs reject `discriminator` and reject non-object roots.** So the
extraction model returns a single flat `ExtractedEvent` with *every* kind-specific field present and
nullable (`symptom_name`, `severity`, `appointment_kind`, `medication_name`, `note_category`, …)
inside an object root `{ "events": [...] }`. The API is what folds those flat fields into the
`details` object for the matching `kind` and drops the rest.

Two model shapes, deliberately. Do not try to make the LLM emit the union, and do not leak the flat
shape to the client. Names differ where the flat model needs a kind prefix that the union doesn't:

| Flat `ExtractedEvent` field | API location |
|---|---|
| `symptom_name` | `details.symptom` |
| `medication_action` | `details.action` |
| `note_category` | `details.category` |
| `occurred_at_precision` | `occurred_at_precision` — **same five values, passed through unchanged.** `"exact" \| "day" \| "week" \| "month" \| "unknown"` on both sides; there is no translation step to get wrong |
| `source_message_handles: ["M12", "M17"]` | resolved to real `source_excerpts[].message_id`; unknown handles are dropped, never fatal |

The field is `source_message_handles`, **not** `source_message_ids`, and that naming is load-bearing:
handles are opaque chunk-local labels (`M1`…`M200`, `E1`…`E40`) that the server maps back to UUIDs.
Never put a UUID in a prompt — models transpose long hex runs, and an `_ids` suffix is exactly the
invitation to try.

---

## Auth

### `POST /api/auth/login`

One shared family credential. There is no users table.

```json
{ "username": "the-shaws", "password": "correct-horse-battery-staple" }
```

**204**, plus:

```
Set-Cookie: penny_session=<signed>; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000
```

The cookie carries `household_id` only, signed with `SESSION_SECRET` via `itsdangerous`. 30 days.

- **401** — `{"detail": "Invalid username or password."}`. **Identical** for an unknown username and
  a wrong password; no user enumeration.
- **429** — `{"detail": "Too many attempts. Try again in a minute."}`. The login endpoint is rate
  limited per IP: one shared password is a single guessable secret with no lockout story.

### `POST /api/auth/logout`

No body. **204**, with the cookie cleared. The client must then call `queryClient.clear()`.

### `GET /api/me`

The session probe the app boots on.

```json
{
  "household": {
    "id": "0d4f8b2e-1c8a-4c1e-9f6a-3b7d2e5a91cc",
    "name": "The Shaws",
    "care_recipient_name": "Margaret",
    "timezone": "Europe/London"
  },
  "counts": { "events": 143, "messages": 2011 }
}
```

**401** when signed out — this is normal on first load, not an error to report.

---

## Household

### `PATCH /api/household`

All fields optional; send only what changed.

```json
{ "name": "The Shaws", "care_recipient_name": "Margaret", "timezone": "Europe/London" }
```

**200** returns the same `household` object as `GET /api/me`.

- **422** — `{"detail": "'Europe/Londn' is not a known timezone."}`. Validated against
  `zoneinfo.available_timezones()` on write.

### `POST /api/household/password`

```json
{ "current_password": "old-passphrase", "new_password": "new-long-passphrase" }
```

**204**. Existing sessions stay valid — the cookie carries `household_id`, not the password — so
nobody is signed out. Re-sharing the new passphrase with the family is manual.

- **401** when `current_password` is wrong.
- **422** when `new_password` is under 12 characters.

### `DELETE /api/household`

Deletes the household and every message, event, member, import and report by cascade. **204**, and
the session cookie is cleared. Irreversible; the UI must type-to-confirm.

---

## Feed

### `GET /api/feed?limit=200&before=2026-07-01T00:00:00Z`

Events, newest first (`occurred_at DESC, id DESC`), excluding deleted ones.

| Param | Default | Notes |
|---|---|---|
| `limit` | `200` | Max `500`. |
| `before` | — | ISO UTC, **exclusive** on `occurred_at`. Omit for the first page. |

```json
{
  "events": [
    {
      "id": "9a1c...",
      "kind": "appointment",
      "occurred_at": "2026-07-17T09:30:00Z",
      "occurred_at_precision": "exact",
      "title": "GP appointment, Dr Aziz",
      "body": "Sarah took Margaret to the GP. Bloods taken, review booked in two weeks.",
      "actor": { "member_id": "3c7e...", "display_name": "Sarah" },
      "details": {
        "appointment_kind": "gp",
        "provider_name": "Dr Aziz",
        "location": "Beech Road Surgery",
        "attendees": ["Sarah", "Margaret"],
        "outcome": "Bloods taken, review in 2 weeks",
        "follow_up_actions": ["Book review appointment for 31 July"],
        "status": "attended"
      },
      "source_excerpts": [
        {
          "message_id": "6f1f...",
          "sent_at": "2026-07-17T08:12:00Z",
          "sender": "Sarah",
          "quote": "Just got back from the GP with Mum, bloods taken, review in 2 weeks"
        }
      ],
      "edited_at": null,
      "created_at": "2026-07-17T10:02:11Z"
    }
  ],
  "next_before": "2026-07-14T19:04:00Z"
}
```

`next_before` is the `occurred_at` of the last event returned, or **`null` when there is no next
page** (fewer than `limit` rows came back). The client stops when it sees `null`.

Known and accepted: `before` is exclusive on `occurred_at` only, so events sharing the exact
boundary timestamp can be skipped across a page edge. With a few hundred events per household and a
day-grouped feed, real keyset pagination protects nothing here.

Reports are **not** in this response. The client interleaves them by date, client-side.

### `GET /api/upcoming`

Everything with `occurred_at > now()`, **ascending** (soonest first), capped at 50. Same `Event`
shape. "Upcoming" is a query, not an entity.

```json
{ "events": [] }
```

An empty list is the common case and must render as an empty state, not a spinner.

### `PATCH /api/events/{id}`

Full trust: anyone signed in edits anything. All fields optional.

```json
{
  "title": "GP appointment, Dr Aziz (rescheduled)",
  "body": "Moved to the 24th.",
  "occurred_at": "2026-07-24T09:30:00Z",
  "occurred_at_precision": "exact",
  "details": { "status": "scheduled" }
}
```

**200** returns the full updated `Event`. `details` is **merged**, not replaced — send only the keys
you are changing. Any edit sets `edited_at`, which permanently protects the row from being
overwritten by a later re-extraction.

- **404** if the event doesn't exist **or** belongs to another household.

### `DELETE /api/events/{id}`

Soft delete (`deleted_at`). **204**. Gone from `/api/feed` and `/api/upcoming` immediately.

- **404** if missing or not yours.

---

## Imports (`.txt` WhatsApp export)

A **two-screen wizard**: preview, then confirm. `dd/mm` vs `mm/dd` is undecidable from an export
spanning under 12 days, and exports contain **no timezone offset at all**. The server suggests;
the user confirms. Guessing silently shifts every date by months.

### `POST /api/imports/preview`

`multipart/form-data` with one part: `file`. Parses in memory, **stores nothing**.

**200**:

```json
{
  "filename": "WhatsApp Chat with Mum's Care.txt",
  "file_sha256": "3b1f0c...",
  "report": {
    "total_lines": 24188,
    "messages": 21044,
    "continuations": 2903,
    "system_lines": 189,
    "media_placeholders": 412,
    "unparsed_lines": 52,
    "unparsed_samples": ["‎<Media omitted>", "17/03/2026, 09:1 - broken line"],
    "detected_format": "%d/%m/%Y, %H:%M",
    "senders": { "Sarah": 9014, "Tom": 7712, "Priya": 4318 },
    "first_sent_at": "2026-01-04T08:11:00Z",
    "last_sent_at": "2026-07-24T21:47:00Z",
    "preview_head": [
      { "sent_at": "2026-01-04T08:11:00Z", "sender": "Sarah", "text": "Morning all" }
    ],
    "preview_tail": [
      { "sent_at": "2026-07-24T21:47:00Z", "sender": "Tom", "text": "Night" }
    ]
  },
  "sniffed": {
    "dayfirst": true,
    "dayfirst_evidence": "day>12",
    "timezone": "Europe/London",
    "timezone_source": "household_default"
  },
  "estimate": {
    "message_count": 21044,
    "estimated_cost_usd": "8.84",
    "estimated_minutes": 9,
    "budget_usd": "25.00",
    "over_budget": false
  }
}
```

`dayfirst_evidence` is `"day>12" | "month>12" | "conflict" | "none"`. Anything other than a clean
`day>12`/`month>12` means the UI must make the user choose explicitly rather than pre-selecting.
`preview_head`/`preview_tail` are 5 messages each, rendered with the chosen timezone applied so the
user can sanity-check the dates before committing.

- **413** — `{"detail": "That file is larger than 25 MB."}`
- **422** — `{"detail": "No WhatsApp messages found in that file."}`

### `POST /api/imports`

`multipart/form-data`: `file`, plus `dayfirst` (`"true"`/`"false"`) and `timezone` (IANA name).
Both are **required** — there is no server-side default, because a wrong guess is silent.

**202**:

```json
{
  "import_id": "b21e4f77-0f2c-4c9e-9a01-88c4d1b2c3f4",
  "message_count": 21044,
  "estimated_cost_usd": "8.84"
}
```

Messages are inserted and extraction runs in `BackgroundTasks`. Poll `GET /api/imports/{id}`.

- **409** — `{"detail": "This export has already been imported."}` (same `file_sha256` for this
  household). Re-uploading a *longer* export of the same chat is fine and expected: it has a
  different hash, and per-message `content_hash` means the overlapping messages don't duplicate.
- **422** — unknown timezone, or `dayfirst` missing.
- **402-adjacent case, expressed as 409** — `{"detail": "That import would cost about $31, over the
  $25 limit."}` when the estimate exceeds `IMPORT_MAX_SPEND_USD`.

### `GET /api/imports/{id}`

```json
{
  "status": "extracting",
  "message_count": 21044,
  "inserted_count": 20887,
  "extracted_count": 6120,
  "error": null
}
```

`status` ∈ `"pending" | "importing" | "extracting" | "complete" | "failed"`. Terminal states are
`complete` and `failed`; stop polling on those. `extracted_count` counts messages with
`extracted_at` set, so a progress bar is `extracted_count / inserted_count`.

A budget abort or a redeploy mid-extraction leaves `extracted_at` NULL on the remainder, and the
hourly cron picks it up — so a stalled import self-heals rather than needing a re-upload. `error`
is a rendered sentence when `status` is `"failed"`, `null` otherwise.

- **404** if missing or not yours.

---

## Reports

### `GET /api/reports`

Newest first.

```json
[
  {
    "id": "77c1...",
    "title": "Week of 13–19 July",
    "period_start": "2026-07-13T00:00:00Z",
    "period_end": "2026-07-20T00:00:00Z",
    "status": "complete",
    "generated_at": "2026-07-20T06:03:12Z"
  }
]
```

`status` ∈ `"pending" | "running" | "complete" | "failed"`. `generated_at` is `null` until complete.

### `GET /api/reports/{id}`

Citations are **resolved server-side**. The model writes opaque `[E3]` handles (never UUIDs — models
transpose long hex runs), and this response maps each handle to a real event so the UI can link it.
Handles that don't resolve are stripped before storage: a dead citation never reaches the client.

```json
{
  "id": "77c1...",
  "title": "Week of 13–19 July",
  "period_start": "2026-07-13T00:00:00Z",
  "period_end": "2026-07-20T00:00:00Z",
  "status": "complete",
  "generated_at": "2026-07-20T06:03:12Z",
  "summary": "Margaret's sleep worsened this week and she saw Dr Aziz on Friday.",
  "urgent_flag": false,
  "urgent_reason": null,
  "sections": [
    {
      "heading": "Sleep",
      "body_markdown": "Margaret was up several times overnight on three occasions [E1] [E2], against a four-week average of one night a week.",
      "citations": [
        {
          "handle": "E1",
          "event_id": "9a1c...",
          "kind": "symptom",
          "occurred_at": "2026-07-14T22:40:00Z",
          "title": "Poor sleep, up 4 times overnight"
        }
      ]
    }
  ],
  "questions_for_the_doctor": ["Could the new amlodipine dose be affecting her sleep?"],
  "watch_items": ["Appetite — two mentions of skipped meals"],
  "data_gaps": ["No blood pressure readings recorded this week"],
  "error": null
}
```

Statistics in the prose are computed in SQL and handed to the model, never counted by it.

- **404** if missing or not yours.

### `POST /api/reports/generate`

No body. **202**:

```json
{ "report_id": "77c1...", "status": "pending" }
```

Poll `GET /api/reports/{id}`.

- **429** — `{"detail": "A report was already generated today."}` Limit is 1/day/household on
  demand; the weekly report is produced by the cron tick.

---

## WhatsApp (GOWA)

### `GET /api/whatsapp/status`

```json
{
  "linked": true,
  "is_connected": true,
  "is_logged_in": true,
  "group_external_id": "120363000000000000@g.us"
}
```

`linked` is Penny's own state (a `whatsapp_links` row exists). `is_connected`/`is_logged_in` are
proxied from GOWA's `/app/status` and are **`false` when GOWA is unreachable**, not an error — the
UI shows "not connected", never a crash. `is_logged_in: false` means the session died and the
family must re-pair by QR: treat it as routine, not exceptional.

`group_external_id` is `null` when unlinked. Poll this at `30_000` on the settings screen only.

### `POST /api/whatsapp/link`

```json
{ "group_external_id": "120363000000000000@g.us" }
```

**204**.

- **400** — `{"detail": "That is not a group chat id (it must end in @g.us)."}` There is **no
  `is_group` field** on a GOWA message event; the suffix is the only signal.
- **409** — `{"detail": "That group is already linked."}`

### `POST /api/whatsapp/webhook`

Called by GOWA, **not** by the browser. No cookie.

```
X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the RAW body, key = WHATSAPP_WEBHOOK_SECRET>
```

Verify the signature over raw bytes **before parsing**, constant-time. GOWA's default secret is
literally `"secret"` — change it.

```json
{
  "event": "message",
  "payload": {
    "id": "3EB0A1B2C3",
    "chat_id": "120363000000000000@g.us",
    "from": "447700900123@s.whatsapp.net",
    "from_name": "Sarah",
    "timestamp": 1784275920,
    "body": "Just got back from the GP with Mum",
    "type": "text"
  }
}
```

**200** `{"status": "ok"}` — returned **before any LLM work**. GOWA retries 5× with a 10s timeout and
exponential backoff, so this handler must be fast and idempotent on `payload.id` (that is what the
unique index on `(household_id, provider_message_id)` is for). A replayed payload inserts once.

Also **200**, deliberately, with `{"status": "ignored", "reason": "unknown_group"}` when the
`chat_id` maps to no household, or `"not_a_group"` when it doesn't end in `@g.us`. Retrying cannot
fix either, and an unknown group **never auto-provisions a household** — that is the tenant-leak
vector. It is logged and dropped.

- **401** on a missing or bad signature. No body detail beyond `{"detail": "Invalid signature."}`.

`image` payloads are polymorphic: a bare `string` with no caption, an `object` with one. v1 stores
`message_type`, leaves `text` null, downloads nothing, and the UI renders "📎 photo (not stored)".
Edits, deletions and reactions are dropped at the adapter.

---

## Members

### `GET /api/members`

```json
[
  {
    "id": "3c7e...",
    "display_name": "Sarah",
    "wa_jid": "447700900123@s.whatsapp.net",
    "wa_lid": "251566778899@lid",
    "message_count": 9014,
    "first_seen_at": "2026-01-04T08:11:00Z",
    "last_seen_at": "2026-07-24T21:47:00Z"
  }
]
```

A family that imports history **and** pairs GOWA will see duplicate people: exports give a display
name with no JID, GOWA gives JIDs. That is expected, visible, and fixed by a merge.

### `POST /api/members/{id}/merge`

```json
{ "into_member_id": "8b02..." }
```

**204**. `{id}` is absorbed into `into_member_id`: messages and events re-point, and the surviving
member keeps whichever of `wa_jid`/`wa_lid` is non-null. Attribution is fixed retroactively without
re-running the LLM.

- **400** when `id == into_member_id`.
- **404** if either member is missing or not yours.

---

## Operational

### `GET /api/health`

No auth. **200** `{"status": "ok"}`. **Must not touch the database** — a DB-dependent healthcheck
turns a Postgres blip into a Railway rollback loop.

### `GET /api/ai/status`

No auth. Reports configuration without leaking the key.

```json
{ "configured": true, "model": "gpt-5.5-2026-04-23" }
```

`model` is `llm_model_extract`.

### `POST /api/internal/tick`

Called by the Railway cron service, hourly. No cookie.

```
X-Penny-Tick-Secret: <INTERNAL_TICK_SECRET>
```

**200**:

```json
{ "households_scanned": 3, "messages_extracted": 118, "reports_generated": 1 }
```

Does the scheduled work: extract where `unextracted_count >= 40` **or**
`oldest_unextracted_age > 6h`, pick up anything unextracted older than 10 minutes (this is what
makes a backfill self-healing across a redeploy), and generate the weekly report on Monday 07:00 in
each household's timezone.

- **401** on a wrong or missing secret.
