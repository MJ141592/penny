# Penny architecture

Penny is a same-origin React and FastAPI application. Families can backfill a WhatsApp `.txt`
export or receive live group messages through GOWA; both paths converge on one idempotent ingestion
boundary and one extraction service.

> [!NOTE]
> This diagram describes the current working tree. Solid green nodes are active in
> `app/main.py`. Dashed amber nodes are inactive: most are implemented routers that are not
> currently registered. Reports have a frontend route, API contract, and database model, but no
> backend router yet.

```mermaid
flowchart TB
    family["Family member<br/>web browser"]
    whatsapp["WhatsApp group"]
    openai["OpenAI Responses API"]

    subgraph railway["Railway project"]
        direction LR

        subgraph penny["penny service - one Docker image"]
            direction TB

            subgraph browser["React 19 SPA"]
                direction LR
                screens["Feed | Import | Settings<br/>Login | Report"]
                query["TanStack Query<br/>cache, polling, optimistic edits"]
                client["Typed API client<br/>same-origin /api"]
                screens --> query --> client
            end

            subgraph fastapi["FastAPI application"]
                direction TB
                middleware["JSON logging<br/>error handlers<br/>dev-only CORS"]
                mounted["Mounted now<br/>health + AI status"]
                spa["SPA catch-all<br/>frontend/dist"]
                session["Session and tenant boundary<br/>signed HttpOnly household_id cookie<br/>HouseholdCtx filters every query"]

                subgraph dormant["Implemented routers - not mounted in app/main.py"]
                    direction LR
                    auth["Auth + /me"]
                    readapi["Feed + upcoming<br/>household + members"]
                    eventapi["Event edit<br/>soft delete"]
                    importapi["Import<br/>preview, start, poll"]
                    waapi["WhatsApp<br/>status, link, relink"]
                    webhook["Webhook<br/>raw-body HMAC verify<br/>validate + adapt"]
                end

                reportapi["Reports API + scheduled generation<br/>contract only; router absent"]
                middleware --> mounted
                middleware -.-> dormant
                middleware -.-> reportapi
                session -.-> dormant
                mounted --> spa
            end

            subgraph ingest["Shared ingestion"]
                direction LR
                txt["WhatsApp export parser<br/>sniff date format<br/>user confirms timezone"]
                contract["InboundMessage contract"]
                seam["ingest_messages<br/>resolve household + member<br/>hash and insert idempotently"]
                txt --> contract --> seam
            end

            subgraph extraction["Incremental extraction"]
                direction LR
                trigger["Detached background task<br/>after request commit"]
                service["Extraction service<br/>per-household advisory lock<br/>30-day budget guard"]
                cursor["Read messages where<br/>extracted_at IS NULL"]
                runner["Chunk + overlap context<br/>concurrent structured calls<br/>validate, merge, deduplicate"]
                gateway["LLM gateway<br/>retry, truncation handling<br/>usage and cost accounting"]
                persist["Persist extraction<br/>protect human edits<br/>stamp processed messages"]
                trigger --> service --> cursor --> runner --> gateway
                runner --> persist
            end
        end

        subgraph gowa["gowa service"]
            direction TB
            bridge["WhatsApp bridge<br/>whatsmeow"]
            pairing["Status + pairing QR API<br/>Basic Auth over private network"]
            chatstore[("Persistent volume<br/>chat storage")]
            bridge --- pairing
            bridge --- chatstore
        end

        postgres[("PostgreSQL 16<br/>households | members | whatsapp_links<br/>messages | llm_runs | events<br/>imports | reports<br/><br/>household_id is the tenant key<br/>unique indexes enforce replay safety")]
    end

    family -->|HTTPS| screens
    client -->|JSON + multipart<br/>cookie credentials| middleware
    spa -->|index.html + assets| family

    client -.-> auth
    client -.-> readapi
    client -.-> eventapi
    client -.-> importapi
    client -.-> waapi
    client -.-> reportapi

    importapi -.->|preview / commit| txt
    webhook -.-> contract
    seam -->|messages + members| postgres
    importapi -.->|accepted import| trigger
    webhook -.->|return 200, then extract| trigger

    service -->|advisory lock + budget query| postgres
    postgres --> cursor
    gateway -->|strict JSON schema request| openai
    openai -->|events + token usage| gateway
    gateway -->|audit every attempt| postgres
    persist -->|events, occurrences,<br/>extracted_at cursor| postgres

    auth -.-> postgres
    readapi -.-> postgres
    eventapi -.-> postgres
    importapi -.-> postgres
    waapi -.-> postgres
    reportapi -.-> postgres

    whatsapp -->|linked-device events| bridge
    bridge -->|signed webhook<br/>private network| webhook
    waapi -.->|status, link, relink<br/>private network| pairing
    bridge -->|whatsmeow session tables| postgres

    classDef active fill:#e9f7ef,stroke:#247a46,color:#12351f,stroke-width:2px;
    classDef dormant fill:#fff5df,stroke:#b7791f,color:#4a3107,stroke-width:2px,stroke-dasharray:6 4;
    classDef external fill:#eef3f8,stroke:#536779,color:#1e2933;
    classDef store fill:#f2ecf8,stroke:#704b8f,color:#2f1f3a,stroke-width:2px;

    class middleware,mounted,spa active;
    class auth,readapi,eventapi,importapi,waapi,webhook,reportapi dormant;
    class family,whatsapp,openai external;
    class postgres,chatstore store;
```

## Runtime status

- `app/main.py` currently includes only the health and AI routers before mounting the SPA.
- Auth, feed, event, household, import, member, webhook, and WhatsApp routers exist in
  `backend/app/routers/` but are not reachable until they are included in `app/main.py`.
- The report screens, API contract, and `reports` table exist; report endpoints and generation are
  not implemented.
- Production builds the SPA in the first Docker stage, copies it into the Python image, runs
  Alembic in Railway's pre-deploy phase, and serves the SPA and API from one origin.

## Architectural invariants

- Every tenant-owned row carries `household_id`; request dependencies apply that boundary to reads
  and writes.
- Live webhooks and file imports normalize to the same `InboundMessage` contract and call the same
  ingestion function.
- Message replay safety lives in PostgreSQL unique indexes, not in request handlers.
- `messages.extracted_at IS NULL` is the resumable extraction cursor.
- Human-edited event fields and soft-deleted event tombstones survive later extraction runs.
- Every LLM attempt is recorded with model, prompt version, usage, latency, cost, and outcome.
