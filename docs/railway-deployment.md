# Railway deployment — Penny + GOWA

Platform facts verified against docs.railway.com and the GOWA source (repo @ `08e005f`, release
v9.0.0) on 2026-07-25. Topology reconciled against
`.context/plans/penny-family-care-coordination-web-app.md`.

Anything this document does not state is **unverified** — treat it as a question for M1b/M2, not as a
fact. Where the original notes and the plan disagreed, the resolution is spelled out below rather
than silently applied.

---

## Reconciliation with the plan

**1. Topology: four services → two. The plan wins.**
The source notes described four services (`web` serving `frontend/dist` via Caddy, `api`, `gowa`,
Postgres). The plan's locked architecture table says **two services plus Postgres**: a single `penny`
service where FastAPI serves `frontend/dist`, plus `gowa`. Same-origin deletes CORS, `SameSite=None`
and CSRF outright, so the plan's shape is the one built here. Every platform-level gotcha from the
notes is carried across with hostnames adapted — most visibly, the GOWA webhook target is now
`http://penny.railway.internal:8000/api/whatsapp/webhook`.

**2. GOWA storage: volume vs Postgres. Both, because they are different databases.**
The notes said GOWA's default `DB_URI` is `file:storages/whatsapp.db` on a volume, and that GOWA's
*separate* `chatstorage.db` is unconditionally SQLite on local disk and cannot be pointed at
Postgres. The plan says `DB_URI=postgres://…`, "never a volume", because losing the whatsmeow session
means a manual QR re-pair that needs the family member's physical phone. Both are right about
different stores. Resolution: **session store on Postgres via `DB_URI`, *and* a small volume mounted
at `/app/storages` for `chatstorage.db`.** See [GOWA storage](#gowa-storage-two-databases-two-answers).
The plan lists this as **open question #1**, to be settled empirically during M1b — see
[`gowa-runbook.md`](./gowa-runbook.md).

---

## Topology

Two services plus Postgres in one Railway project. Only `penny` gets a permanent public domain.

| Service | Source | Public domain | Notes |
|---|---|---|---|
| `penny` | repo, root `/` | **Yes** | Root `Dockerfile`: builds `frontend/dist`, FastAPI serves it same-origin alongside `/api/*` |
| `gowa` | Docker image, pinned tag | **No** — temporarily yes, once, to pair | WhatsApp bridge + small volume |
| `Postgres` | Railway template | No | Penny's data *and* GOWA's whatsmeow session |

Wiring:

- `gowa` → `http://penny.railway.internal:8000/api/whatsapp/webhook` (private)
- `penny` → `${{Postgres.DATABASE_URL}}` and `http://gowa.railway.internal:3000`
- `gowa` → `${{Postgres.PG*}}`, hand-composed (see below — you cannot use `DATABASE_URL`)

No worker service: user-triggered work runs in FastAPI `BackgroundTasks`. The plan's M5 adds a
scheduled `POST /api/internal/tick` described as a "Railway cron service"; the source notes cover no
cron behaviour at all, so **how cron is configured on Railway is unverified here** — settle it in M5.

### Why not a separate frontend service

The notes' `web` service (Railpack auto-detects Vite → Caddy serves `dist`) is genuinely less work to
set up, and it decouples a CSS change from an API restart. It was still dropped:

- **What you gain by folding it in:** one origin. No CORS middleware, no `SameSite=None`, no CSRF
  token dance for the session cookie, no `VITE_API_URL`, one domain, one TLS cert, one healthcheck.
  The SPA and the API ship in the same image, so they cannot version-skew.
- **What it costs:** the Dockerfile needs a Node build stage (Railpack's zero-config Vite detection
  is no longer doing that work for you), and every frontend-only change rebuilds the image and
  restarts the API process. At 1–5 test families that restart is invisible.
- **Reversibility:** re-adding a `web` service later is purely additive — the notes' Railpack recipe
  (Root Directory `/frontend`, watch paths `/frontend/**`, generate a domain, set `VITE_API_URL`)
  still works. You would be buying back CORS and cookie-domain problems, so don't do it without a
  reason.

---

## 1. Create the project

```sh
brew install railway
railway login
railway init -n penny
railway link
```

Verify your GitHub account at railway.com/verify **before** deploying. An unverified account gets a
"Limited Trial" with *"restricted outbound network access and only a limited set of ports"* — that
will break GOWA's WhatsApp websocket. Go straight to Hobby ($5/mo).

## 2. Postgres

```sh
railway add --database postgres
```

Exposes `DATABASE_URL`, `PGHOST`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`. Reference from
other services as `${{Postgres.DATABASE_URL}}` (namespace = service name as shown in the dashboard,
case-sensitive).

Delete the TCP Proxy in the Postgres service's networking settings unless you need external access —
it's enabled by default, so your DB is publicly reachable, and traffic through it bills as egress.

## 3. `gowa` service

```sh
railway add --image aldinokemal2104/go-whatsapp-web-multidevice:v9.0.0 --service gowa
railway volume add --service gowa --mount-path /app/storages
```

**Leave the start command blank.** The image's `ENTRYPOINT` is `/entrypoint.sh` and `CMD` is already
`rest`. Setting the start command to just `rest` replaces the entrypoint and crash-loops (Docker
tries to `exec rest`, which doesn't exist). If you must set one, use the full `/entrypoint.sh rest`.

Variables:

```env
APP_PORT=3000
PORT=3000
APP_HOST=[::]
APP_BASIC_AUTH=penny:<password-no-colon-no-comma>
APP_UI_ENABLED=false
DB_URI=postgres://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:5432/${{Postgres.PGDATABASE}}?sslmode=disable
WHATSAPP_WEBHOOK=http://penny.railway.internal:8000/api/whatsapp/webhook
WHATSAPP_WEBHOOK_SECRET=<random-32-bytes>
WHATSAPP_WEBHOOK_EVENTS=message
WHATSAPP_AUTO_DOWNLOAD_MEDIA=false
WHATSAPP_ACCOUNT_VALIDATION=false
APP_TRUSTED_PROXIES=0.0.0.0/0
```

Healthcheck path: `/health` (unauthenticated, returns `OK`/200).

Notes:

- `APP_UI_ENABLED=false` skips the v9 dashboard, which otherwise fetches `gowa-ui.html` from GitHub
  at boot and re-checks every 3h. Failure is graceful (fallback HTML, API unaffected), but there's no
  reason to take the egress, the auto-pull, or an unauthenticated dashboard.
- `APP_BASIC_AUTH` parses with `strings.Split(s, ":")` and `Fatalln`s if the result isn't exactly 2
  parts — **a `:` in the password is a hard startup crash**. `,` separates user pairs, so avoid that
  too. Generate the password from an alphabet that excludes both.
- `WHATSAPP_AUTO_DOWNLOAD_MEDIA=false` prevents unbounded growth in `/app/statics/media`. v1 does not
  download media at all: it stores `message_type`, leaves `text` NULL, and the UI shows
  "📎 photo (not stored)".
- `WHATSAPP_WEBHOOK_SECRET` defaults to the literal string `"secret"` upstream. Setting it is not
  optional.
- Outbound messages you send are also delivered to the webhook with `is_from_me: true`. There is no
  filter — drop them in the handler.
- The webhook URL hardcodes `:8000`. That is only correct because `PORT=8000` is set as a service
  variable on `penny` (see gotcha 4).

### GOWA storage: two databases, two answers

GOWA keeps two separate stores, and the deployment treats them differently:

| Store | What's in it | Where it goes | Why |
|---|---|---|---|
| **Session** (`DB_URI`) | whatsmeow device session + Signal keys | **Railway Postgres** | Losing it is a full manual QR re-pair needing the family member's physical phone. Nothing reconstructs it. Volumes are single-instance and take downtime on every redeploy; Postgres decouples session state from the filesystem. |
| **Chat storage** (`chatstorage.db`) | device registry, chat history | **Volume at `/app/storages`** | Unconditionally SQLite on local disk per the source notes; it cannot be pointed at Postgres. Losing it costs history, not the session. |

Two lib/pq footguns, both hard startup failures:

1. **The URI must start with `postgres:`.** Railway's `DATABASE_URL` is `postgresql://`, which fails
   GOWA's prefix check and **panics at startup**. You cannot use `${{Postgres.DATABASE_URL}}` here.
2. **It needs `?sslmode=disable`.** lib/pq v1.12.3 defaults to `sslmode=require` with no plaintext
   fallback, and Railway's internal Postgres does not terminate TLS.

Hence the hand-composed value, exactly:

```
DB_URI=postgres://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:5432/${{Postgres.PGDATABASE}}?sslmode=disable
```

Open items to close during M1b (the plan's open question #1 — "resolve empirically", not up front):

- Whether `chatstorage.db` actually lands under `/app/storages` is **unverified**; the notes state
  only that it is local-disk SQLite. Confirm with `railway ssh --service gowa` or
  `railway volume browse /` after pairing, and move the mount path if it lands elsewhere.
  `CHAT_STORAGE_MAX_OPEN_CONNS` is documented upstream as *"Max concurrent SQLite connections"*,
  which is the hint that it is SQLite-pinned.
- The composed URI points GOWA at the **same logical database** as Penny. The plan asks for a
  separate logical DB or schema. If they share one, GOWA's `whatsmeow_*` tables sit in Penny's
  database and Alembic `autogenerate` will happily propose dropping them unless `env.py` filters
  unknown tables — verify before the first autogenerate run in M2.

## 4. `penny` service

Source: this repo, Root Directory `/` (the repo root), built from the root `Dockerfile` (multi-stage:
Node stage builds `frontend/dist`, Python stage runs FastAPI and serves those files). The Dockerfile,
`.dockerignore` and `railway.json` land in M2 and are owned by Track B.

`railway.json` at the repo root:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": {
    "builder": "DOCKERFILE",
    "dockerfilePath": "Dockerfile"
  },
  "deploy": {
    "startCommand": "gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind [::]:$PORT --workers 2 --timeout 120",
    "preDeployCommand": ["alembic upgrade head"],
    "healthcheckPath": "/api/health",
    "healthcheckTimeout": 300,
    "restartPolicyType": "ON_FAILURE"
  }
}
```

- Whether a root `Dockerfile` is auto-detected or needs an explicit builder key in the `build` block
  is **unverified** — the source notes only document `builder = "RAILPACK"` for Railpack-built
  services. Confirm in M2.
- With one service built from the repo root there is no useful watch-path split; the whole repo is
  the build context. (Under the old four-service layout each service set its own `watchPatterns`, and
  the config-file path in Settings → Config-as-code was **not** relative to Root Directory — it had to
  be given as an absolute repo path. Both stop mattering at the root.)
- `preDeployCommand` runs `alembic upgrade head` on the `penny` service **only**. Nothing else in the
  project runs migrations.

Variables:

```env
ENV=production
PORT=8000
DATABASE_URL=${{Postgres.DATABASE_URL}}
OPENAI_API_KEY=<key>
SESSION_SECRET=<random-32-bytes>
GOWA_URL=http://gowa.railway.internal:3000
GOWA_BASIC_AUTH=penny:<same-password-as-APP_BASIC_AUTH>
WHATSAPP_WEBHOOK_SECRET=<same-random-32-bytes-as-gowa>
INTERNAL_TICK_SECRET=<random-32-bytes>     # M5, when the cron caller exists
```

- `ENV=production` is what turns off the dev-only CORS middleware. Same-origin serving means the app
  needs no CORS at all in production, and leaving it on would widen the surface for nothing.
- **Note the asymmetry with `gowa`:** `penny` *can* take `${{Postgres.DATABASE_URL}}` directly —
  `Settings` rewrites `postgres://` / `postgresql://` to `postgresql+asyncpg://` on the way in. GOWA
  cannot, and panics on `postgresql://`. Same database, two different scheme requirements.
- Defaults that usually need no override: `SERVE_FRONTEND=true` (mounts `frontend/dist` from the API
  process), `LLM_MODEL_EXTRACT` / `LLM_MODEL_REPORT`, `IMPORT_MAX_SPEND_USD`, `DEFAULT_TIMEZONE`. Full
  table in the repo `README.md`.

---

## Gotchas that will actually bite

**1. GOWA ignores `$PORT`.** It reads `APP_PORT` (viper, no prefix). Railway's injected `PORT` does
nothing. Set `APP_PORT=3000` explicitly, and also set a `PORT=3000` service variable so the
healthcheck and `${{gowa.PORT}}` references resolve.

**2. `APP_HOST=[::]`, never `::`.** GOWA builds the listen address as `APP_HOST + ":" + APP_PORT`.
Bare `::` produces `:::3000`, which fails `net.SplitHostPort` with "too many colons in address" and
crashes at startup. Bracketed `[::]` parses correctly and Go sets `IPV6_V6ONLY=0` for the wildcard,
giving dual-stack. The default `0.0.0.0` is IPv4-only.

**3. uvicorn cannot dual-stack bind.** Railway documents this explicitly: *"Most servers handle this
automatically when listening on `::`, but some, like Uvicorn, do not."* `uvicorn --host ::` binds
IPv6-only and breaks the public healthcheck. Use `gunicorn ... --bind [::]:$PORT` (as in the
`railway.json` above), or `uvicorn --host "" --port $PORT` with an *empty string*. Railway's own
troubleshooting page still says `--host 0.0.0.0` — that page only considers public networking; don't
copy it. Environments created after 2025-10-16 are dual-stack, so `0.0.0.0` would in fact work today;
`[::]` is the portable choice and costs nothing.

**4. `${{svc.PORT}}` does not auto-resolve.** Railway: *"It does not automatically resolve to the port
the service is listening on, nor does it resolve to the `PORT` environment variable injected into the
service at runtime."* You must set `PORT` manually as a service variable on anything called privately
— hence `PORT=8000` on `penny` and `PORT=3000` on `gowa` — or you get
`http://penny.railway.internal:` with an empty port. This is the #1 silent private-networking
failure.

**5. Private networking is runtime-only.** Not available during build. Migrations must run in
`preDeployCommand` or the start command, never in a Dockerfile step.

**6. Volumes mean downtime on every redeploy.** Railway *"prevent[s] multiple deployments from being
active and mounted to the same service"* — healthchecks don't give you zero-downtime here. Volumes
also aren't mounted at build time or pre-deploy time, and resize is up-only. This is now confined to
`gowa`: `penny` has no volume, so the user-facing service keeps normal rolling deploys, and a `gowa`
restart only pauses ingest.

**7. Healthchecks come from `healthcheck.railway.app`.** If you add FastAPI's `TrustedHostMiddleware`
to `penny`, allowlist that hostname or you'll get "healthcheck failed with status 400". Healthchecks
are not continuous — Railway stops polling once the deploy goes live. `/api/health` must not touch
the database, or a Postgres blip becomes a rollback loop.

**8. `/health` and `/statics` sit outside GOWA's basic auth.** They're registered before the auth
middleware. That means the login QR PNG is publicly fetchable by URL. The filename is a UUID and the
file is deleted after ~30s, so it's tolerable for a one-time link — just don't leave a public domain
attached afterwards.

**9. Pin the image tag.** `latest` is a mutable manifest re-pointed on every release, and releases
land every 1–3 weeks. v8→v9 moved the UI out of the image entirely. But treat the pin as short-lived:
the "client outdated (405)" failure — where WhatsApp rejects a stale hardcoded client version and QR
generation silently stops working — is *only* fixable by upgrading. Budget a monthly bump and be able
to bump within hours. Railway's Docker Hub auto-update with a "patches only" semver policy is a
reasonable middle ground.

**10. GOWA's `DB_URI` cannot be `${{Postgres.DATABASE_URL}}`.** See
[GOWA storage](#gowa-storage-two-databases-two-answers) — `postgresql://` panics at startup, and the
missing `?sslmode=disable` fails against Railway's non-TLS internal Postgres.

---

## Webhook contract

`POST` with `Content-Type: application/json`, header `X-Hub-Signature-256: sha256=<lowercase hex>`,
HMAC-SHA256 over the **raw body bytes** keyed on `WHATSAPP_WEBHOOK_SECRET`. Verify against the raw
body, not a re-serialized object, and verify **before** parsing. 5 retries with exponential backoff
(1s, 2s, 4s, 8s); any non-2xx counts as failure, so return 200 fast and process async.

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

- **There is no `is_group` field.** Detect a group by `payload.chat_id` ending in `@g.us`.
- For a group, `chat_id` is the group and `from` is the individual participant.
- Only `id`, `timestamp`, `is_from_me`, `chat_id`, `from` are guaranteed. Everything else is
  conditionally set — `from_name` is omitted entirely when empty. Model them as optional.
- **Key person identity on `from_lid` as well as `from`.** Phone and display name are best-effort
  enrichment; keying on one field alone silently fragments a person into two members.
- No top-level `timestamp` on `message` events (other event types do have one).
- `image` is polymorphic: `str` when there's no caption, `dict` when there is.
- Idempotency is on `payload.id`, backed by the `(household_id, provider_message_id)` unique index.

## Sending to a group

```
POST http://gowa.railway.internal:3000/send/message
Content-Type: application/json
Authorization: Basic <base64(user:pass)>

{"phone": "120363402106XXXXX@g.us", "message": "hello"}
```

Field is `phone` even for a group JID. Pass the full `...@g.us` explicitly. Optional:
`reply_message_id`, `mentions` (array of phone numbers, or the literal `"@everyone"`).

## Cost

RAM $10/GB/mo, CPU $20/vCPU/mo, volume $0.15/GB/mo, egress $0.05/GB, billed per minute on measured
consumption.

Rough estimate for the two-service shape: `penny` ~250 MB (the SPA is static files served by the
existing process, not a second container), `gowa` ~120 MB, Postgres ~180 MB ≈ 0.55 GB → ~$5.50/mo
RAM, ~$1.60/mo CPU, plus the volume. The volume now holds only `chatstorage.db`, so start at the
smallest size Railway will give you and grow — resize is up-only. At 1 GB that's $0.15/mo, so
**≈ $7.25/mo**. Folding the frontend in saves the ~30 MB Caddy container from the original
four-service estimate (~$8/mo). On Hobby ($5 sub, $5 included usage) that's a ~$7 bill. Pro at $20/mo
flat would fully absorb it if you want volume auto-backups or private registries. This sits inside
the plan's ~$10–20/mo Railway line; OpenAI is the dominant cost, not hosting.

You pay for running containers regardless of traffic. Don't enable Serverless — private-network
traffic and DB connection pools prevent sleep anyway, and the first request to a slept service can
return a 502.

Set a hard usage limit at railway.com/workspace/usage (minimum $10; takes workloads offline when hit)
or an email-only alert.

## Useful commands

```sh
railway logs --service gowa
railway logs --network --peer gowa --port 3000     # private-net flow debugging
railway ssh --service gowa                          # shell inside the container
railway variable set APP_PORT=3000 --service gowa
railway redeploy --service gowa --yes
railway volume browse /                             # inspect the volume
```

`railway run` executes on *your* machine with remote env vars injected — it cannot reach
`*.railway.internal`. Use `railway ssh` for anything on the private network.

---

Pairing a phone is a separate, manual, once-per-account procedure: see
[`gowa-runbook.md`](./gowa-runbook.md).
