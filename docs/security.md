# Security posture

What is automated, what is consciously accepted, and what a human still has to click in the GitHub
UI. Written 2026-07-25 while closing finding 8 of the security review ("repository security
automation is incomplete, and `npm audit` reports five high advisories").

Repository: `github.com/MJ141592/penny` (public). Every navigation path below is written out in
full because none of it can be set from a checked-out repo.

---

## 1. Dependency updates

[`.github/dependabot.yml`](../.github/dependabot.yml) covers five ecosystems:

| Ecosystem | Directory | Cadence | What it watches |
|---|---|---|---|
| `uv` | `/backend` | weekly (Mon) | `pyproject.toml` + `uv.lock` |
| `npm` | `/frontend` | weekly (Mon) | `package.json` + `package-lock.json` |
| `github-actions` | `/` | weekly (Mon) | action versions in `.github/workflows` |
| `docker` | `/` | weekly (Mon) | base images in the production `Dockerfile` |
| `docker-compose` | `/` | monthly | local-dev Postgres image |

`uv`, not `pip`: the `pip` ecosystem does not maintain `uv.lock`, and CI runs `uv sync --frozen`, so
a `pip` PR would arrive permanently red.

Minor and patch updates are grouped into **one PR per ecosystem per week**, and security updates into
a second group, so a PR titled "security" always means an advisory. Majors are deliberately left
ungrouped — a breaking change should arrive alone and be read alone. Open-PR limits are 1–3 per
ecosystem. On a one-person project the real failure mode is not a missed bump; it is twenty PRs a
week, which is how the one that mattered gets closed unread.

**This file does nothing until Dependabot alerts are switched on in the UI — see §4.**

## 2. The CI audit gate

`.github/workflows/ci.yml` ends the frontend job with **Audit (fails only on new high advisories)**.

`npm audit --audit-level=high` on its own is all-or-nothing: it either gets ignored, or it goes red
for advisories that have already been reviewed and accepted — which is the same thing a week later.
The gate instead parses `npm audit --json`, collapses each advisory's dependency chain to a single
GHSA id, and fails **only on high/critical ids that are not in its `ACCEPTED` map**.

- `ACCEPTED` is **empty** today. Everything the review found was fixed rather than waived.
- A registry outage prints a warning and passes rather than reporting "no vulnerabilities".
- An `ACCEPTED` id that no longer appears prints a warning so the list does not silently rot.

**To accept a new advisory:** add `['GHSA-…', 'why it does not apply here + when to revisit']` to
`ACCEPTED` in `ci.yml`, and add a row to §3 below. Both, always — the map holds the id, this file
holds the reasoning.

Not covered: Python advisories. There is no `pip-audit` step, because adding one means adding a
backend dev dependency and that was outside this change. Dependabot security updates (§4) cover the
backend once enabled, and the review found no known Python vulnerabilities.

## 3. Accepted risks

**None open.** All five high `npm audit` advisories were fixed:

| Advisory | Package | Fix |
|---|---|---|
| [GHSA-qwww-vcr4-c8h2](https://github.com/advisories/GHSA-qwww-vcr4-c8h2) | `react-router` | upgraded 7.18.1 → 8.3.0 |
| [GHSA-52cp-r559-cp3m](https://github.com/advisories/GHSA-52cp-r559-cp3m) | `js-yaml` | `overrides` → `^4.3.0` |
| [GHSA-mh99-v99m-4gvg](https://github.com/advisories/GHSA-mh99-v99m-4gvg) | `brace-expansion` | `overrides` → `^5.0.8` |
| (transitive) | `minimatch`, `@redocly/openapi-core` | resolved by the `brace-expansion` override |

Two notes for whoever reads `frontend/package.json` next and wonders about the `overrides` block:

- **Why overrides rather than a version bump.** `openapi-typescript@7.13.0` is the latest release and
  depends on `@redocly/openapi-core@^1.34.6`, which pins `js-yaml` and `minimatch` to *exact*
  versions. `npm audit fix` therefore reports a fix as available and then changes nothing. The
  overrides are a patch-level nudge inside the same major for `js-yaml`, and a major bump for
  `brace-expansion` that is safe because 5.0.8 still ships a CommonJS entry point for `minimatch@5`'s
  `require`. Verified by running the real `openapi-typescript` CLI against a YAML spec afterwards.
  **Remove both overrides** once `openapi-typescript` ships a release built on `@redocly/openapi-core@2.x`,
  which already uses `js-yaml@5` and `picomatch`.
- **The react-router major.** The advisory only affects the unstable RSC APIs, which this SPA does not
  use, so the upgrade was optional and was only taken because it verified clean: strict `tsc`, lint,
  production build, then a headless-Chrome run of the fixtures build showing `/`, `/settings`,
  `/reports/:id` and the 404 route rendering identically to 7.18.1, plus client-side `<Link>`
  navigation and the signed-out `<Navigate>` gate landing on `/login` without a full page load.
  `react-router@8` requires Node ≥ 22.22.0 and React ≥ 19.2.7; `node:22-alpine` in the `Dockerfile`
  is 22.23.1 and React is 19.2.8. **If the Dockerfile's Node major is ever lowered, the frontend
  build breaks** — that coupling is now real. The one imported API not exercised in a browser is
  `useRouteError` (the route error boundary); it is unchanged in v8.

## 4. Checklist: settings only a human can change

All of these are UI-only. Tick them in one sitting; it takes about ten minutes.

- [ ] **Dependency graph, Dependabot alerts, Dependabot security updates**
  `github.com/MJ141592/penny` → **Settings** → **Code security** (older UI: *Code security and
  analysis*) → enable **Dependency graph**, **Dependabot alerts**, **Dependabot security updates**.
  Direct link: `https://github.com/MJ141592/penny/settings/security_analysis`.
  Without this, `.github/dependabot.yml` still opens version-update PRs but you get no advisory
  alerts and no automatic security PRs.

- [ ] **Code scanning (CodeQL)**
  Same page → **Code scanning** → **CodeQL analysis** → **Set up** → **Default**. Confirm the
  detected languages include **JavaScript/TypeScript** and **Python**. Default setup needs no
  workflow file, so it will not collide with `ci.yml`.

- [ ] **Secret scanning + push protection**
  Same page → **Secret scanning** → Enable, and enable **Push protection**. Free on public repos.
  This is the control that stops the next `.env` paste, which matters more here than any of the
  above: the review confirmed no live credentials are in history *today*.

- [ ] **Private vulnerability reporting**
  Same page → **Private vulnerability reporting** → **Enable**. Gives a stranger who finds a hole in
  a family health app somewhere to report it other than a public issue.

- [ ] **Branch ruleset on `main` — force-push and deletion only**
  **Settings** → **Rules** → **Rulesets** → **New ruleset** → **New branch ruleset**. Name it
  `main`, set **Enforcement status: Active**, target **Include default branch**, and tick
  **Restrict deletions** and **Block force pushes**. Leave **Require a pull request** and **Require
  status checks** *off*.
  Deliberate: this project's workflow is committing straight to `main`. Requiring status checks on a
  branch you push directly to blocks the push (the check cannot have run yet), and requiring PRs
  would stop the workflow outright. Blocking deletion and force-push costs nothing and removes the
  only two irreversible mistakes. If the project ever moves to PRs, come back and add **Require a
  pull request before merging** plus required checks **`backend`** and **`frontend`**.

- [ ] **Restrict Actions to what CI actually uses**
  **Settings** → **Actions** → **General** → **Actions permissions** → *Allow MJ141592, and select
  non-MJ141592, actions and reusable workflows*, then in **Allow specified actions** list:
  `actions/checkout@*`, `actions/setup-node@*`, `astral-sh/setup-uv@*`. A compromised third-party
  action runs with the workflow token; an allow-list is the cheap version of pinning by SHA.

- [ ] **Workflow token defaults**
  Same page → **Workflow permissions** → **Read repository contents and packages permissions**, and
  untick **Allow GitHub Actions to create and approve pull requests**. `ci.yml` already declares
  `permissions: contents: read`, but the repo default applies to anything added later.

- [ ] **Fork PR approval** (public repo)
  Same page → **Fork pull request workflows from outside collaborators** → **Require approval for
  all outside collaborators**.

## 5. Personal email address in commit metadata

The review's public-repo check found no real credentials in any reachable blob, but did note that a
personal Gmail address is visible in commit metadata (`git log` author fields, and therefore the
GitHub API and every clone). It is not a credential, but it is a permanent, machine-harvestable link
between this project and a personal inbox. Three options, in increasing cost:

1. **Accept it.** The address is already public elsewhere; nothing more leaks by leaving it.
2. **Stop adding to it** (recommended, ~2 minutes, no history rewrite).
   **Settings** (account, not repo) → **Emails** → tick **Keep my email addresses private** and
   **Block command line pushes that expose my email**. That page shows your
   `ID+USERNAME@users.noreply.github.com` address; then, in this repo:
   `git config user.email "ID+MJ141592@users.noreply.github.com"`.
   Past commits keep the old address; new ones do not.
3. **Rewrite history.** `git filter-repo --mailmap`, then force-push. This changes every commit SHA,
   invalidates existing clones and any link that references a SHA, requires temporarily lifting the
   force-push rule from §4, and the old objects stay reachable through the GitHub API until GitHub
   support garbage-collects them on request. For a solo project whose author name is on the repo
   anyway, this is rarely worth it.

## 6. What CI already enforces

`.github/workflows/ci.yml`, on every push and PR, with a read-only token:

- **backend** — `uv sync --frozen` (a stale `uv.lock` fails CI), `ruff check`, `ruff format --check`,
  `pytest -m "not live"`. The `live` marker filter is load-bearing: CI has no `OPENAI_API_KEY` and
  must never acquire one.
- **frontend** — `npm ci`, `oxlint`, `tsc -b && vite build`, then the audit gate in §2.
