# Variable manifest

Every environment variable the two deployed services need, and — for each one — why it is allowed
to exist. This is the authoritative list: if a variable is not below, it must not be set on Railway.

Four classes, and the class is the whole argument:

| Class | Meaning | Who supplies it |
|---|---|---|
| **ENV-SPECIFIC** | A human has to bring it; nothing can invent it. **Only `OPENAI_API_KEY` and `APP_PUBLIC_URL` may be this.** | you |
| **SECRET** | Per-deployment, but *minted*, not copied. Replicating this deployment never requires learning an existing value. | `infra/provision.py` |
| **INFRA** | Follows from the project's own shape: the database, the sidecar's internal DNS name, which environment this is. | `infra/provision.py` |
| **CODE-DEFAULT** | Lives in `backend/app/config.py`. **Do not set it in Railway.** | the repo |

The classification is not maintained here by hand. `backend/app/config.py` declares every field
through `env_specific()` / `secret()` / `infra()` / `code_default()` and exposes the result as
`SETTING_CLASSES`; `backend/tests/test_config_surface.py` fails if a new field skips the decision or
if `ENV_SPECIFIC_FIELDS` grows past those two names. `infra/provision.py --self-test` re-reads
`config.py` and fails if the provisioner's copy of the CODE-DEFAULT list has drifted, or if the
provisioner ever tries to write one.

---

## `penny` (FastAPI + the SPA)

| Variable | Class | Value | Notes |
|---|---|---|---|
| `OPENAI_API_KEY` | **ENV-SPECIFIC** | `sk-…` | The one value a teammate must be told. Set it over stdin, never as an argument: `printf %s '<key>' \| railway variable set OPENAI_API_KEY --stdin --service penny`. Unset is legal — the app boots and serves the feed with extraction and transcription off. |
| `APP_PUBLIC_URL` | **ENV-SPECIFIC** | `https://<domain>` | The other one. Goes into the WhatsApp welcome message, so a wrong value is a dead link in the first thing a family ever sees. Derived from `--domain` by the provisioner. No trailing slash. |
| `SESSION_SECRET` | **SECRET** | `token_hex(32)` | Signs the HttpOnly cookie carrying `household_id`. **Rotating it logs every household out.** Required in production: `startup_checks` refuses to boot without it, below 32 chars, or on any literal published in `.env.example`. |
| `WHATSAPP_WEBHOOK_SECRET` | **SECRET** | `token_hex(32)` | HMAC-SHA256 key for `X-Hub-Signature-256`. **Must equal `WHATSAPP_WEBHOOK_SECRET` on `gowa`.** If they differ, penny rejects every inbound message and ingest silently stops. GOWA's shipped default is the literal `secret`. |
| `GOWA_BASIC_AUTH` | **SECRET** | `penny:<token_hex(32)>` | Credentials for calling the sidecar. **Must equal `APP_BASIC_AUTH` on `gowa`.** Exactly one `:`, and no `,` — GOWA `Fatalln`s on anything else. Hex satisfies both by construction. |
| `INTERNAL_TICK_SECRET` | **SECRET** | `token_hex(32)` | Shared secret for whatever calls `POST /api/internal/tick`. Generated now so the cron service has something to match when it lands. |
| `ENV` | INFRA | `production` | Turns off the dev-only CORS middleware (production is same-origin and needs none) and makes the startup secret checks fatal rather than advisory. |
| `PORT` | INFRA | `8000` | Set by hand because `${{penny.PORT}}` does **not** auto-resolve, and because gowa's webhook URL hardcodes `:8000`. |
| `DATABASE_URL` | INFRA | `${{Postgres.DATABASE_URL}}` | A reference, stored literally. `Settings` rewrites `postgres://` / `postgresql://` to `postgresql+asyncpg://` on the way in — which is why penny can use this directly and gowa cannot. |
| `GOWA_URL` | INFRA | `http://gowa.railway.internal:3000` | Private networking; runtime only, never resolvable at build time. |

Generate any SECRET with:

```sh
python -c "import secrets; print(secrets.token_hex(32))"
```

## `gowa` (WhatsApp bridge, `aldinokemal2104/go-whatsapp-web-multidevice:v9.0.0`)

| Variable | Class | Value | Notes |
|---|---|---|---|
| `WHATSAPP_WEBHOOK_SECRET` | **SECRET** | same as penny's | See above. Both sides or neither. |
| `APP_BASIC_AUTH` | **SECRET** | same as penny's `GOWA_BASIC_AUTH` | Parsed with `strings.Split(s, ":")` and `Fatalln` unless the result is exactly 2 parts. A `:` in the password is a hard startup crash; `,` separates user pairs. |
| `DB_URI` | INFRA | `postgres://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:5432/${{Postgres.PGDATABASE}}?sslmode=disable` | Hand-composed, and every part is load-bearing. `postgres:` because GOWA prefix-checks the scheme and **panics** on `postgresql://`; `?sslmode=disable` because lib/pq defaults to `require` with no plaintext fallback and Railway's internal Postgres does not terminate TLS. **You cannot use `${{Postgres.DATABASE_URL}}` here.** |
| `APP_PORT` | INFRA | `3000` | GOWA reads this (viper, no prefix). Railway's injected `PORT` does nothing for it. |
| `PORT` | INFRA | `3000` | So healthchecks and `${{gowa.PORT}}` references resolve. |
| `APP_HOST` | INFRA | `[::]` | **With the brackets.** GOWA builds the listen address as `APP_HOST + ":" + APP_PORT`, so a bare `::` yields `:::3000` and dies in `net.SplitHostPort`. |
| `APP_UI_ENABLED` | INFRA | `false` | The v9 dashboard otherwise fetches `gowa-ui.html` from GitHub at boot and re-checks every 3h, and it is unauthenticated. |
| `APP_TRUSTED_PROXIES` | INFRA | `0.0.0.0/0` | Railway's edge is the only thing in front of it. |
| `WHATSAPP_WEBHOOK` | INFRA | `http://penny.railway.internal:8000/api/whatsapp/webhook` | The `:8000` is only correct because `PORT=8000` is set on `penny`. |
| `WHATSAPP_WEBHOOK_EVENTS` | INFRA | `message,group.joined,group.participants` | `group.joined` is what onboarding fires on. Without it, a family who add Penny to a group get nothing until somebody speaks. |
| `WHATSAPP_AUTO_DOWNLOAD_MEDIA` | INFRA | `false` | Keeps `/app/statics/media` from growing without bound; v1 does not store media. |
| `WHATSAPP_ACCOUNT_VALIDATION` | INFRA | `false` | — |

`gowa` also needs a volume at `/app/storages` for `chatstorage.db`, which is unconditionally SQLite
on local disk. The whatsmeow **session** is the thing that costs a physical-phone re-pair, and it
lives in Postgres via `DB_URI`, not on the volume.

## Injected by Railway — set none of these

`RAILWAY_*`, `RAILWAY_PRIVATE_DOMAIN`, `RAILWAY_PUBLIC_DOMAIN`, `RAILWAY_VOLUME_*`, and the
Postgres addon's own `PG*` / `DATABASE_URL` / `DATABASE_PUBLIC_URL`.

---

## CODE-DEFAULT — set none of these in Railway

Each of these has its production value in `backend/app/config.py`. Setting one in Railway forks the
running configuration away from the repository: `config.py` stops describing what production does,
and the change exists in a dashboard field with no diff attached to it. Change the code and deploy.

Removing one from Railway is behaviourally a no-op — a variable set to its own default and an unset
variable are the same process.

`LLM_MODEL_EXTRACT` · `LLM_MODEL_REPORT` · `SESSION_MAX_AGE_DAYS` · `IMPORT_MAX_SPEND_USD` ·
`LLM_MONTHLY_BUDGET_USD_PER_HOUSEHOLD` · `TRANSCRIBE_VOICE_NOTES` · `TRANSCRIPTION_MODEL` ·
`TRANSCRIPTION_MAX_SECONDS` · `TRANSCRIPTION_MAX_BYTES` · `DEFAULT_TIMEZONE` · `SERVE_FRONTEND` ·
`EXTRACT_MIN_UNEXTRACTED` · `EXTRACT_MAX_AGE_HOURS` · `ONBOARDING_ENABLED` ·
`ONBOARDING_MAX_HOUSEHOLDS` · `ONBOARDING_PLACEHOLDER_CARE_RECIPIENT` ·
`STARTUP_QUIET_PERIOD_SECONDS` · `JOIN_BURST_WINDOW_SECONDS`

`infra/provision.py --prune` deletes exactly this list from both services and nothing else.

### The one exception: operational levers

`ONBOARDING_ENABLED` and `ONBOARDING_MAX_HOUSEHOLDS` are on that list *and* are the two levers you
reach for in an incident. There is no contradiction:

- Set them in Railway **only to hold a value other than the default**, as a deliberate, temporary
  override — `railway variable set ONBOARDING_ENABLED=false --service penny` is an incident action
  and takes effect in one restart, faster than a deploy.
- Do **not** leave them sitting at their default value, which is what production did. That is not
  configuration; it is a second source of truth waiting to disagree with the first.
- When the incident is over, either delete the override or land the new value in `config.py`.

## Local development

`.env.example` is the same manifest in `.env` form, with the same four sections and dev-appropriate
values. `cp .env.example .env` and fill in the two ENV-SPECIFIC values.

`PENNY_TEST_DATABASE_URL` appears there and nowhere here: it is read only by the test suite, and a
production process never looks at it.
