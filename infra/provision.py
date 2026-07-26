#!/usr/bin/env python3
"""Build the whole Penny deployment on Railway, from nothing, repeatably.

    infra/provision.py --workspace <id-or-name> --domain pennyai.chat

The contract this script exists to keep: a teammate with the repository and two values — an
OpenAI key and a domain — can stand up an identical deployment without being told anything
else. Everything that is not one of those two is either generated here, derived from the
project's own topology, or a default that lives in `backend/app/config.py` and is deliberately
NOT set on Railway at all.

WHY PYTHON AND NOT BASH. Railway's cross-service references are spelled `${{Postgres.PGUSER}}`,
and that string must reach Railway's API byte-for-byte. Under `sh` it is a brace expansion
wrapped around a parameter expansion: bash rewrites it, silently, and you get a service that
boots with an empty password and fails ten minutes later somewhere unrelated. This already
happened once. Here every command is an argv LIST handed to `subprocess.run(..., shell=False)`,
so no shell ever parses it; `${{...}}` cannot be expanded because nothing capable of expanding
it is in the pipeline. The quoting problem is not solved carefully, it is deleted.

Two more things Python buys that matter more than the extra lines:
  * `railway ... --json` parses into a dict, so "does this already exist?" is a real question
    with a real answer, which is what makes the script idempotent rather than merely re-runnable.
  * Secrets go to `railway variable set KEY --stdin`, so they never appear in an argv that `ps`
    can read, and never in an echoed line that ends up in a transcript.

IDEMPOTENCE, precisely. Re-running converges; it does not duplicate and it does not rotate.
  * Project, services, volume, domain: created only when absent.
  * Generated secrets: minted only when the key is absent on the service. A second run does not
    reissue SESSION_SECRET, because that would log every household out.
  * Literal infra values (ENV, PORT, GOWA_URL, ...): enforced — reset when they have drifted.
  * Reference values (`${{Postgres.DATABASE_URL}}`): set only when absent, because Railway's API
    returns them RESOLVED, so a comparison against the literal would never match and every run
    would rewrite them and trigger a deploy.

WHAT IT WILL NOT DO. It never sets anything classified `code_default()` in `backend/app/config.py`
(`--self-test` proves that, offline). It never writes a secret to a file. It never leaves gowa
holding a public domain.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# --- Fixed topology. None of this is a per-deployment choice. ---------------------------------

PENNY_SERVICE = "penny"
GOWA_SERVICE = "gowa"
DEFAULT_PROJECT = "penny"
DEFAULT_ENVIRONMENT = "production"

# Pinned, never `latest`: that tag is a mutable manifest re-pointed every 1-3 weeks, and v8 -> v9
# moved the UI out of the image entirely. Budget a monthly bump; see docs/deployment.md.
GOWA_IMAGE = "aldinokemal2104/go-whatsapp-web-multidevice:v9.0.0"
# chatstorage.db is unconditionally SQLite on local disk. The whatsmeow SESSION lives in Postgres
# via DB_URI, which is the one that costs a physical-phone re-pair if it is lost.
GOWA_MOUNT_PATH = "/app/storages"

# `${{svc.PORT}}` does not auto-resolve to the port a service listens on, so both ports are set
# by hand and the webhook URL below hardcodes penny's.
PENNY_PORT = "8000"
GOWA_PORT = "3000"
WEBHOOK_PATH = "/api/whatsapp/webhook"

# GOWA splits APP_BASIC_AUTH on ":" and Fatallns unless it gets exactly two parts, and uses ","
# to separate user pairs. token_hex() emits [0-9a-f] only, so both are structurally impossible.
GOWA_BASIC_AUTH_USER = "penny"
# token_hex(32) -> 64 chars, comfortably over startup_checks.MIN_SECRET_CHARS (32).
SECRET_BYTES = 32
MIN_SECRET_CHARS = 32


class Mode(Enum):
    """How hard a variable is held."""

    # Reset whenever the live value differs. For literals we own outright.
    ENFORCE = "enforce"
    # Write only when the key is missing. For generated secrets (a rewrite would rotate them)
    # and for `${{...}}` references (the API reports them resolved, so they never compare equal).
    IF_ABSENT = "if-absent"


@dataclass(frozen=True)
class Var:
    key: str
    value: str
    mode: Mode
    secret: bool = False


def _service_domain(service: str) -> str:
    return f"http://{service}.railway.internal"


# --- The manifest. infra/variables.md is the prose version of exactly this. --------------------


def penny_vars(*, domain: str, postgres_service: str) -> list[Var]:
    return [
        # INFRA
        Var("ENV", "production", Mode.ENFORCE),
        Var("PORT", PENNY_PORT, Mode.ENFORCE),
        Var("GOWA_URL", f"{_service_domain(GOWA_SERVICE)}:{GOWA_PORT}", Mode.ENFORCE),
        # penny CAN take DATABASE_URL directly — Settings rewrites postgres:// and postgresql://
        # to postgresql+asyncpg:// on the way in. gowa cannot; see gowa_vars().
        Var("DATABASE_URL", "${{" + postgres_service + ".DATABASE_URL}}", Mode.IF_ABSENT),
        # ENV-SPECIFIC
        Var("APP_PUBLIC_URL", f"https://{domain}", Mode.ENFORCE),
    ]


def gowa_vars(*, postgres_service: str) -> list[Var]:
    pg = postgres_service
    # Hand-composed, and every part of it is load-bearing:
    #   "postgres:"  — GOWA prefix-checks the scheme and PANICS on Railway's "postgresql://",
    #                  which is why ${{Postgres.DATABASE_URL}} cannot be used here.
    #   sslmode=disable — lib/pq defaults to require with no plaintext fallback, and Railway's
    #                  internal Postgres does not terminate TLS.
    db_uri = (
        f"postgres://${{{{{pg}.PGUSER}}}}:${{{{{pg}.PGPASSWORD}}}}"
        f"@${{{{{pg}.PGHOST}}}}:5432/${{{{{pg}.PGDATABASE}}}}?sslmode=disable"
    )
    return [
        # GOWA reads APP_PORT (viper, no prefix); Railway's injected PORT does nothing for it.
        # PORT is set as well so healthchecks and ${{gowa.PORT}} references resolve.
        Var("APP_PORT", GOWA_PORT, Mode.ENFORCE),
        Var("PORT", GOWA_PORT, Mode.ENFORCE),
        # WITH the brackets. GOWA builds the listen address as APP_HOST + ":" + APP_PORT, so a
        # bare "::" yields ":::3000" and dies in net.SplitHostPort.
        Var("APP_HOST", "[::]", Mode.ENFORCE),
        # The v9 dashboard otherwise fetches gowa-ui.html from GitHub at boot and re-checks every
        # 3h. No reason to take the egress, the auto-pull, or an unauthenticated dashboard.
        Var("APP_UI_ENABLED", "false", Mode.ENFORCE),
        Var("APP_TRUSTED_PROXIES", "0.0.0.0/0", Mode.ENFORCE),
        Var(
            "WHATSAPP_WEBHOOK",
            f"{_service_domain(PENNY_SERVICE)}:{PENNY_PORT}{WEBHOOK_PATH}",
            Mode.ENFORCE,
        ),
        # group.joined is what onboarding actually fires on; without it a family adding Penny to
        # a group gets nothing until somebody speaks.
        Var("WHATSAPP_WEBHOOK_EVENTS", "message,group.joined,group.participants", Mode.ENFORCE),
        # Keeps /app/statics/media from growing without bound. v1 stores message_type and shows
        # "photo (not stored)" rather than downloading.
        Var("WHATSAPP_AUTO_DOWNLOAD_MEDIA", "false", Mode.ENFORCE),
        Var("WHATSAPP_ACCOUNT_VALIDATION", "false", Mode.ENFORCE),
        Var("DB_URI", db_uri, Mode.IF_ABSENT),
    ]


# Set in Railway ONLY when a run needs to mint them; never rewritten once present.
#   name -> (penny key, gowa key or None)
# A key appearing on both sides MUST hold the same value on both, or the pairing silently
# half-works: penny would reject every webhook HMAC, or gowa would 401 every outbound send.
SHARED_SECRETS: dict[str, tuple[str, str | None]] = {
    "session_secret": ("SESSION_SECRET", None),
    "internal_tick_secret": ("INTERNAL_TICK_SECRET", None),
    "webhook_secret": ("WHATSAPP_WEBHOOK_SECRET", "WHATSAPP_WEBHOOK_SECRET"),
    "basic_auth": ("GOWA_BASIC_AUTH", "APP_BASIC_AUTH"),
}

# Variables that only restate a `code_default()` in backend/app/config.py. Setting one forks the
# running configuration away from the repo, which is the exact failure this whole exercise is
# about, so the script never writes them — and `--prune` deletes them if a previous hand-built
# deployment left them behind. Nothing here changes behaviour by being removed: an unset variable
# and a variable set to its own default are the same process.
#
# ONBOARDING_ENABLED and ONBOARDING_MAX_HOUSEHOLDS are on this list and are ALSO the two
# operational levers. That is not a contradiction: you set them when you want a value other than
# the default, and docs/deployment.md tells you to. What must not happen is them sitting there
# at their default value pretending to be configuration.
CODE_DEFAULT_KEYS: frozenset[str] = frozenset(
    {
        "LLM_MODEL_EXTRACT",
        "LLM_MODEL_REPORT",
        "SESSION_MAX_AGE_DAYS",
        "IMPORT_MAX_SPEND_USD",
        "LLM_MONTHLY_BUDGET_USD_PER_HOUSEHOLD",
        "TRANSCRIBE_VOICE_NOTES",
        "TRANSCRIPTION_MODEL",
        "TRANSCRIPTION_MAX_SECONDS",
        "TRANSCRIPTION_MAX_BYTES",
        "DEFAULT_TIMEZONE",
        "SERVE_FRONTEND",
        "EXTRACT_MIN_UNEXTRACTED",
        "EXTRACT_MAX_AGE_HOURS",
        "ONBOARDING_ENABLED",
        "ONBOARDING_MAX_HOUSEHOLDS",
        "ONBOARDING_PLACEHOLDER_CARE_RECIPIENT",
        "STARTUP_QUIET_PERIOD_SECONDS",
        "JOIN_BURST_WINDOW_SECONDS",
    }
)


# --- Plumbing ---------------------------------------------------------------------------------


class Failure(Exception):
    """A stop-now condition with an operator-readable explanation."""


class Console:
    """stdout, with every known secret scrubbed on the way out.

    Nothing prints a secret except `report_generated_secrets`, once, deliberately. Everything
    else — including a CLI error message that happened to quote the value it was given — goes
    through here first.
    """

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def guard(self, value: str) -> str:
        if len(value) >= 8:
            self._secrets.add(value)
        return value

    def scrub(self, text: str) -> str:
        for value in self._secrets:
            text = text.replace(value, "<redacted>")
        return text

    def say(self, text: str = "") -> None:
        print(self.scrub(text))

    def step(self, text: str) -> None:
        self.say(f"  {text}")

    def head(self, text: str) -> None:
        self.say(f"\n== {text}")

    def warn(self, text: str) -> None:
        print(self.scrub(f"!! {text}"), file=sys.stderr)


class Railway:
    """The railway CLI, as argv lists. No shell, ever."""

    def __init__(self, console: Console, *, dry_run: bool) -> None:
        self.console = console
        self.dry_run = dry_run

    def _run(self, args: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["railway", *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def read(self, *args: str) -> Any:
        """A read-only `--json` command. Runs even under --dry-run: the plan must be real."""
        done = self._run([*args, "--json"], None)
        if done.returncode != 0:
            raise Failure(
                f"railway {' '.join(args)} failed:\n{self.console.scrub(done.stderr.strip())}"
            )
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError as exc:
            raise Failure(f"railway {' '.join(args)} returned non-JSON: {exc}") from exc

    def try_read(self, *args: str) -> Any | None:
        try:
            return self.read(*args)
        except Failure:
            return None

    def write(self, *args: str, stdin: str | None = None, label: str | None = None) -> str:
        """A MUTATING command. `stdin` keeps secrets out of argv, where `ps` can read them."""
        shown = label or f"railway {' '.join(args)}"
        if self.dry_run:
            self.console.step(f"[dry-run] {self.console.scrub(shown)}")
            return ""
        self.console.step(self.console.scrub(shown))
        done = self._run(list(args), stdin)
        if done.returncode != 0:
            raise Failure(
                f"{self.console.scrub(shown)} failed:\n"
                f"{self.console.scrub((done.stderr or done.stdout).strip())}"
            )
        # `railway variable set` echoes the value it just stored. Never return it to a caller
        # that might print it; callers here only need "it worked".
        return ""


# --- Secret generation ------------------------------------------------------------------------


def new_secret() -> str:
    """64 lowercase hex characters. No ':' and no ',' by construction, so GOWA cannot choke."""
    return secrets.token_hex(SECRET_BYTES)


def new_basic_auth(user: str = GOWA_BASIC_AUTH_USER) -> str:
    pair = f"{user}:{new_secret()}"
    check_basic_auth(pair)
    return pair


def check_basic_auth(pair: str) -> None:
    """The two rules GOWA enforces with Fatalln, checked before we can trigger them."""
    if pair.count(":") != 1:
        raise Failure("GOWA basic auth must contain exactly one ':' — GOWA exits on anything else")
    if "," in pair:
        raise Failure("GOWA basic auth must contain no ',' — that separates user pairs")
    user, password = pair.split(":", 1)
    if not user or len(password) < MIN_SECRET_CHARS:
        raise Failure(f"GOWA basic auth password must be at least {MIN_SECRET_CHARS} characters")


# --- Pure planning (exercised offline by --self-test) ------------------------------------------


def plan_variables(live: dict[str, str], desired: list[Var]) -> list[Var]:
    """Which of `desired` still has to be written, given what the service already has."""
    todo = []
    for var in desired:
        if var.mode is Mode.IF_ABSENT:
            if var.key not in live:
                todo.append(var)
        elif live.get(var.key) != var.value:
            todo.append(var)
    return todo


def plan_prune(live: dict[str, str]) -> list[str]:
    """Keys present on the service that only restate a code default."""
    return sorted(key for key in live if key in CODE_DEFAULT_KEYS)


def code_default_keys_from_source(config_py: Path) -> frozenset[str]:
    """CODE-DEFAULT field names read straight out of backend/app/config.py.

    The point is drift: `config.py` is the classification's source of truth (it exposes
    `SETTING_CLASSES`), and this script hardcodes a copy so it can stay stdlib-only. --self-test
    reads the real file and fails if the two ever disagree, which is cheaper than discovering it
    when a new default silently starts being written into Railway.
    """
    source = config_py.read_text(encoding="utf-8")
    names = re.findall(r"^ {4}(\w+)\s*:[^=\n]+=\s*code_default\(", source, re.MULTILINE)
    return frozenset(name.upper() for name in names)


# --- Preflight ---------------------------------------------------------------------------------


def preflight(rw: Railway, console: Console, workspace: str) -> None:
    console.head("Preflight")

    if shutil.which("railway") is None:
        raise Failure(
            "the railway CLI is not on PATH.\n"
            "  macOS:  brew install railway\n"
            "  other:  https://docs.railway.com/guides/cli"
        )
    console.step("railway CLI found")

    who = rw.try_read("whoami")
    if who is None:
        raise Failure("not logged in to Railway. Run: railway login")
    console.step(f"logged in as {who.get('email') or who.get('name') or 'unknown'}")

    # railway.json must not carry a startCommand. Railway runs that field WITHOUT a shell, so
    # "$PORT" reaches gunicorn as four literal characters and every replica dies with
    # "'$PORT' is not a valid port number". The Dockerfile CMD wraps it in `sh -c` and is the
    # single start command. Catch a reintroduction here rather than in a rollback at 1am.
    railway_json = REPO_ROOT / "railway.json"
    if railway_json.exists():
        config = json.loads(railway_json.read_text(encoding="utf-8"))
        if "startCommand" in config.get("deploy", {}):
            raise Failure(
                "railway.json sets deploy.startCommand. Railway runs it without a shell, so "
                "$PORT will not expand and every replica will die. Delete the field — the "
                "Dockerfile CMD is the start command."
            )
        console.step("railway.json has no startCommand (correct)")

    # Best-effort workspace validation. A brand-new account has no projects and therefore no
    # workspaces to enumerate, so an empty list is not evidence of anything.
    projects = rw.try_read("list") or []
    workspaces = {}
    for project in projects:
        space = project.get("workspace") or {}
        if space.get("id"):
            workspaces[space["id"]] = space.get("name", "")
    if workspaces and workspace not in workspaces and workspace not in workspaces.values():
        known = "\n".join(f"    {wid}  {name}" for wid, name in sorted(workspaces.items()))
        raise Failure(
            f"workspace {workspace!r} is not one you can see. Known workspaces:\n{known}\n"
            "  Pass the ID, not the display name, if the name is ambiguous."
        )
    console.step(f"workspace {workspace!r} accepted")


# --- Project, services, volume, domain ----------------------------------------------------------

TRIAL_EXPIRED_HELP = (
    "Railway refused to create the project: this workspace's trial has expired.\n"
    "  The personal workspace is the usual culprit. Create the project in a paid workspace and\n"
    "  pass its ID with --workspace, or upgrade at https://railway.com/workspace/plans"
)


def ensure_project(rw: Railway, console: Console, *, name: str, workspace: str, env: str) -> None:
    console.head(f"Project {name!r}")

    linked = rw.try_read("status")
    if linked and linked.get("name") == name and linked.get("deletedAt") is None:
        console.step(f"already linked to {name} ({linked['id']})")
        return

    existing = next(
        (
            p
            for p in (rw.try_read("list") or [])
            if p.get("name") == name and p.get("deletedAt") is None
        ),
        None,
    )
    if existing:
        console.step(f"project exists ({existing['id']}); linking this directory to it")
        rw.write(
            "link",
            "--project",
            existing["id"],
            "--environment",
            env,
            "--workspace",
            workspace,
        )
        return

    try:
        rw.write("init", "--name", name, "--workspace", workspace, "--json")
    except Failure as exc:
        if "trial has expired" in str(exc).lower():
            raise Failure(TRIAL_EXPIRED_HELP) from exc
        raise
    console.step(f"created project {name!r}")


def service_names(rw: Railway) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in (rw.try_read("service", "list") or [])}


def ensure_postgres(rw: Railway, console: Console) -> str:
    """Returns the Postgres service's name, because `${{Name.PGUSER}}` is case-sensitive."""
    console.head("Postgres")
    for name in service_names(rw):
        if name.lower().startswith("postgres"):
            console.step(f"database service {name!r} already present")
            return name
    rw.write("add", "--database", "postgres", "--json")
    # A dry run never creates it, so report the name Railway would have used.
    return next(
        (n for n in service_names(rw) if n.lower().startswith("postgres")),
        "Postgres",
    )


def ensure_gowa(rw: Railway, console: Console, *, postgres_service: str) -> bool:
    """Creates the gowa service with its non-secret variables. Returns True if it was created."""
    console.head(f"Service {GOWA_SERVICE!r}")
    if GOWA_SERVICE in service_names(rw):
        console.step("already present")
        return False

    args = ["add", "--image", GOWA_IMAGE, "--service", GOWA_SERVICE]
    for var in gowa_vars(postgres_service=postgres_service):
        args += ["-v", f"{var.key}={var.value}"]
    # Leave the start command blank: the image's ENTRYPOINT is /entrypoint.sh with CMD `rest`,
    # and setting the start command to `rest` replaces the entrypoint and crash-loops.
    rw.write(*args, "--json")
    console.step(f"created from {GOWA_IMAGE}")
    return True


def ensure_volume(rw: Railway, console: Console) -> None:
    console.head(f"Volume {GOWA_MOUNT_PATH} on {GOWA_SERVICE!r}")
    volumes = (rw.try_read("volume", "list") or {}).get("volumes", [])
    for volume in volumes:
        if volume.get("serviceName") == GOWA_SERVICE and volume.get("mountPath") == GOWA_MOUNT_PATH:
            console.step(f"already mounted ({volume['name']})")
            return

    # `railway volume add` has NO --service flag — verified against `railway volume add --help`
    # on CLI 5.27.0, whatever the docs example says. It attaches to whichever service this
    # directory is LINKED to, so the link is the argument.
    rw.write("service", "link", GOWA_SERVICE)
    try:
        rw.write("volume", "add", "-m", GOWA_MOUNT_PATH, "--json")
    finally:
        rw.write("service", "link", PENNY_SERVICE)


def ensure_penny(rw: Railway, console: Console, *, domain: str, postgres_service: str) -> bool:
    console.head(f"Service {PENNY_SERVICE!r}")
    if PENNY_SERVICE in service_names(rw):
        console.step("already present")
        return False
    args = ["add", "--service", PENNY_SERVICE]
    for var in penny_vars(domain=domain, postgres_service=postgres_service):
        args += ["-v", f"{var.key}={var.value}"]
    rw.write(*args, "--json")
    console.step("created")
    return True


def ensure_source(rw: Railway, console: Console, *, repo: str | None, branch: str) -> None:
    """Point penny at GitHub, so a deploy is a git push and nothing lives only on a laptop."""
    console.head("Deploy source")
    if repo is None:
        console.step("no --repo given; penny will be deployed by upload (railway up)")
        return
    service = service_names(rw).get(PENNY_SERVICE, {})
    current = (service.get("source") or {}).get("repo")
    if current == repo:
        console.step(f"already connected to {repo}")
        return
    rw.write(
        "service",
        "source",
        "connect",
        "--repo",
        repo,
        "--branch",
        branch,
        "--service",
        PENNY_SERVICE,
        "--json",
    )
    if not rw.dry_run:
        console.step(f"connected to {repo}@{branch}")


def ensure_domain(rw: Railway, console: Console, *, domain: str) -> dict[str, Any] | None:
    console.head(f"Domain {domain}")
    listed = rw.try_read("domain", "list", "--service", PENNY_SERVICE) or {}
    if any(d.get("domain") == domain for d in listed.get("domains", [])):
        console.step("already attached")
    else:
        rw.write("domain", domain, "--service", PENNY_SERVICE, "--port", PENNY_PORT, "--json")
        console.step("attached")

    # gowa must NEVER keep a public domain: /health and /statics are registered before its basic
    # auth, so the login QR PNG is publicly fetchable by URL. It gets one for the ten minutes it
    # takes to pair, and then it is deleted (docs/gowa-runbook.md).
    gowa_domains = (rw.try_read("domain", "list", "--service", GOWA_SERVICE) or {}).get(
        "domains", []
    )
    if gowa_domains:
        console.warn(
            f"{GOWA_SERVICE} has a public domain ({', '.join(d['domain'] for d in gowa_domains)}). "
            "Its /health and /statics sit OUTSIDE basic auth, so the login QR is publicly "
            f"fetchable. Delete it: railway domain delete <domain> --service {GOWA_SERVICE} --yes"
        )

    return (rw.try_read("domain", "status", domain, "--service", PENNY_SERVICE) or {}).get("domain")


# --- Variables ----------------------------------------------------------------------------------


def live_variables(rw: Railway, service: str) -> dict[str, str]:
    """Every variable on a service, RESOLVED. Values are secret; only keys are ever printed."""
    data = rw.try_read("variable", "list", "--service", service)
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def set_variable(rw: Railway, service: str, var: Var) -> None:
    """One variable. Secrets go over stdin so they never touch argv or a terminal echo."""
    common = ["--service", service, "--skip-deploys", "--json"]
    if var.secret:
        rw.write(
            "variable",
            "set",
            var.key,
            "--stdin",
            *common,
            stdin=var.value,
            label=f"railway variable set {var.key} --stdin --service {service}  (value hidden)",
        )
    else:
        rw.write("variable", "set", f"{var.key}={var.value}", *common)


def apply_variables(rw: Railway, console: Console, service: str, desired: list[Var]) -> bool:
    live = live_variables(rw, service)
    todo = plan_variables(live, desired)
    if not todo:
        console.step(f"{service}: {len(desired)} variables already correct")
        return False
    for var in todo:
        set_variable(rw, service, var)
    console.step(f"{service}: wrote {len(todo)} of {len(desired)}")
    return True


def reconcile_secrets(
    rw: Railway,
    console: Console,
    *,
    penny_live: dict[str, str],
    gowa_live: dict[str, str],
    openai_key: str | None,
) -> tuple[list[Var], list[Var], dict[str, str]]:
    """Decide every secret's value once, for both services.

    Returns (penny writes, gowa writes, secrets minted THIS RUN). Existing values are reused
    verbatim and never rewritten — a rewrite of SESSION_SECRET logs every household out, and a
    rewrite of the webhook secret breaks ingest until both sides land.
    """
    penny_writes: list[Var] = []
    gowa_writes: list[Var] = []
    minted: dict[str, str] = {}

    for name, (penny_key, gowa_key) in SHARED_SECRETS.items():
        on_penny = penny_live.get(penny_key)
        on_gowa = gowa_live.get(gowa_key) if gowa_key else None

        if on_penny and on_gowa and on_penny != on_gowa:
            # The silent half-broken state: penny rejects every webhook HMAC, or gowa 401s every
            # outbound send. penny is the side the app reads, so it wins and gowa converges.
            console.warn(
                f"{penny_key} on {PENNY_SERVICE} and {gowa_key} on {GOWA_SERVICE} DISAGREE. "
                f"They must match. Copying {PENNY_SERVICE}'s value to {GOWA_SERVICE}."
            )
            on_gowa = None

        value = on_penny or on_gowa
        if value is None:
            value = new_basic_auth() if name == "basic_auth" else new_secret()
            minted[penny_key] = value
        if name == "basic_auth":
            check_basic_auth(value)
        console.guard(value)

        if not on_penny:
            penny_writes.append(Var(penny_key, value, Mode.IF_ABSENT, secret=True))
        if gowa_key and not on_gowa:
            gowa_writes.append(Var(gowa_key, value, Mode.IF_ABSENT, secret=True))

    # ENV-SPECIFIC. The one value no script can invent, and the only reason a teammate needs a
    # conversation. Never rewritten from the environment on a re-run: rotating a key by accident
    # is a support incident, and `railway variable set OPENAI_API_KEY --stdin` is the way to do
    # it on purpose.
    if "OPENAI_API_KEY" in penny_live:
        console.step("OPENAI_API_KEY already set; leaving it alone")
    elif openai_key:
        console.guard(openai_key)
        penny_writes.append(Var("OPENAI_API_KEY", openai_key, Mode.IF_ABSENT, secret=True))
    else:
        console.warn(
            "OPENAI_API_KEY is not set and none was supplied. Penny will boot and serve the feed "
            "with extraction disabled. Set it with:\n"
            "     printf %s '<key>' | railway variable set OPENAI_API_KEY --stdin --service penny"
        )

    return penny_writes, gowa_writes, minted


def prune(rw: Railway, console: Console, service: str, live: dict[str, str]) -> None:
    stale = plan_prune(live)
    if not stale:
        console.step(f"{service}: nothing to prune")
        return
    for key in stale:
        rw.write("variable", "delete", key, "--service", service, "--json")
    console.step(f"{service}: deleted {len(stale)} variables that only restated a code default")


# --- Reporting ------------------------------------------------------------------------------------


def report_generated_secrets(console: Console, minted: dict[str, str]) -> None:
    if not minted:
        console.head("Secrets")
        console.step("nothing minted this run; every secret was already set")
        return

    # The ONLY place a secret is printed, and the only chance to capture it: it is already stored
    # on Railway, and it is never written to a file in this repo.
    print("\n" + "=" * 78)
    print("GENERATED SECRETS — SHOWN ONCE. Put them in the password manager NOW.")
    print("They are already set on Railway; this script will not print them again.")
    print("=" * 78)
    for key, value in minted.items():
        print(f"{key}={value}")
    print("=" * 78 + "\n")


def report_manual_steps(console: Console, *, domain: str, dns: dict[str, Any] | None) -> None:
    console.head("Still to do BY HAND — the deployment is not finished without these")

    console.say("\n  1. Point DNS at Railway.")
    records = (dns or {}).get("dnsRecords") or []
    if records:
        for record in records:
            kind = str(record.get("recordType", "")).replace("DNS_RECORD_TYPE_", "")
            console.say(
                f"       {kind:6s} {record.get('name', '@'):20s} -> {record.get('requiredValue')}"
            )
    else:
        console.say(f"       railway domain status {domain} --service {PENNY_SERVICE} --json")
    console.say("     TLS is issued only after this propagates.")

    console.say("\n  2. Pair the WhatsApp account — docs/gowa-runbook.md.")
    console.say("     Needs a physical phone and a SECONDARY WhatsApp account, never a primary.")
    console.say(f"     It attaches a temporary public domain to {GOWA_SERVICE}; DELETE IT after,")
    console.say("     because /health and /statics sit outside GOWA's basic auth.")

    console.say("\n  3. Set an OpenAI spend cap.")
    console.say("     https://platform.openai.com/settings/organization/limits")
    console.say("     The one guard no bug in this repository can bypass.")

    console.say("\n  4. Verify — docs/deployment.md#verify.")
    console.say(f"       curl -fsS https://{domain}/api/health")
    console.say(f"       curl -fsS https://{domain}/ | head -5      # the SPA")


# --- Offline self-test ----------------------------------------------------------------------------


def self_test() -> int:
    """Everything that can be proved without touching Railway. No network, no mutation."""
    checks: list[tuple[str, Any]] = []

    value = new_secret()
    checks.append(
        ("secret is 64 hex chars", len(value) == 64 and re.fullmatch(r"[0-9a-f]+", value))
    )
    checks.append(("secret clears the 32-char floor", len(value) >= MIN_SECRET_CHARS))
    checks.append(("secret has no ':' or ','", ":" not in value and "," not in value))

    pair = new_basic_auth()
    checks.append(("basic auth has exactly one ':'", pair.count(":") == 1))
    checks.append(("basic auth has no ','", "," not in pair))
    for bad in ("penny:a:b", "penny:with,comma", "penny:short", "nocolon"):
        try:
            check_basic_auth(bad)
        except Failure:
            checks.append((f"rejects basic auth {bad!r}", True))
        else:
            checks.append((f"rejects basic auth {bad!r}", False))

    # The reference must survive verbatim — this is the whole reason the script is Python.
    gowa = {v.key: v.value for v in gowa_vars(postgres_service="Postgres")}
    db_uri = gowa["DB_URI"]
    checks.append(
        ("DB_URI keeps ${{Postgres.PGPASSWORD}} intact", "${{Postgres.PGPASSWORD}}" in db_uri)
    )
    checks.append(
        ("DB_URI uses the postgres: scheme GOWA demands", db_uri.startswith("postgres://"))
    )
    checks.append(("DB_URI disables sslmode", db_uri.endswith("?sslmode=disable")))
    checks.append(("APP_HOST keeps its brackets", gowa["APP_HOST"] == "[::]"))
    checks.append(
        ("gowa webhook targets penny's private port", ":8000/api/" in gowa["WHATSAPP_WEBHOOK"])
    )
    penny = {v.key: v.value for v in penny_vars(domain="example.test", postgres_service="Postgres")}
    checks.append(
        ("DATABASE_URL is a reference", penny["DATABASE_URL"] == "${{Postgres.DATABASE_URL}}")
    )
    checks.append(
        ("APP_PUBLIC_URL follows the domain", penny["APP_PUBLIC_URL"] == "https://example.test")
    )

    # Idempotence, as arithmetic.
    desired = penny_vars(domain="example.test", postgres_service="Postgres")
    checks.append(
        ("empty service needs every variable", len(plan_variables({}, desired)) == len(desired))
    )
    settled = {v.key: v.value for v in desired}
    checks.append(("converged service needs none", plan_variables(settled, desired) == []))
    drifted = dict(settled, ENV="dev")
    checks.append(
        (
            "drifted ENFORCE value is rewritten",
            [v.key for v in plan_variables(drifted, desired)] == ["ENV"],
        )
    )
    resolved = dict(settled, DATABASE_URL="postgresql://real:secret@host/db")
    checks.append(("resolved reference is NOT rewritten", plan_variables(resolved, desired) == []))

    # The script must never write a variable that only restates a code default.
    written = {v.key for v in desired} | {v.key for v in gowa_vars(postgres_service="Postgres")}
    written |= {k for k, _ in SHARED_SECRETS.values()} | {"OPENAI_API_KEY"}
    checks.append(("script writes no CODE-DEFAULT key", not (written & CODE_DEFAULT_KEYS)))

    config_py = REPO_ROOT / "backend" / "app" / "config.py"
    if config_py.exists():
        from_source = code_default_keys_from_source(config_py)
        missing = from_source - CODE_DEFAULT_KEYS
        checks.append(
            (f"CODE_DEFAULT_KEYS matches config.py ({len(from_source)} fields)", not missing)
        )
        if missing:
            print(f"    config.py has code_default fields unknown here: {sorted(missing)}")
        checks.append(
            ("script writes nothing config.py calls code_default", not (written & from_source))
        )

    checks.append(
        (
            "prune only touches known code defaults",
            plan_prune({"SERVE_FRONTEND": "true", "ENV": "production"}) == ["SERVE_FRONTEND"],
        )
    )

    failed = 0
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
        failed += 0 if ok else 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


# --- Entry point ------------------------------------------------------------------------------


def default_repo() -> str | None:
    """owner/repo from `origin`, so the common case needs no flag."""
    done = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/\s]+?)(?:\.git)?$", done.stdout.strip())
    return match.group(1) if match else None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infra/provision.py",
        description="Provision (or reconcile) the Penny deployment on Railway. Idempotent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "OPENAI_API_KEY is read from the environment ONLY — never a flag, because argv is\n"
            "visible to `ps` and lands in shell history.\n\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "  infra/provision.py --workspace <id> --domain pennyai.chat\n"
        ),
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get("RAILWAY_WORKSPACE"),
        help="Railway workspace ID (or unambiguous name). Env: RAILWAY_WORKSPACE",
    )
    parser.add_argument(
        "--domain",
        default=os.environ.get("PENNY_DOMAIN"),
        help="Public domain for penny, e.g. pennyai.chat. Env: PENNY_DOMAIN",
    )
    parser.add_argument("--project", default=os.environ.get("PENNY_PROJECT", DEFAULT_PROJECT))
    parser.add_argument(
        "--environment", default=os.environ.get("PENNY_ENVIRONMENT", DEFAULT_ENVIRONMENT)
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("PENNY_REPO", "<origin>"),
        help="GitHub owner/repo for autodeploys (default: this checkout's origin). "
        "Pass --repo '' to deploy by upload instead.",
    )
    parser.add_argument("--branch", default=os.environ.get("PENNY_BRANCH", "main"))
    parser.add_argument(
        "--prune",
        action="store_true",
        help="Delete variables that only restate a default in backend/app/config.py.",
    )
    parser.add_argument("--no-deploy", action="store_true", help="Provision but do not deploy.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read Railway, print every mutation that WOULD run, change nothing.",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="Run the offline checks and exit. No network."
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return self_test()

    console = Console()
    missing = [n for n in ("workspace", "domain") if not getattr(args, n)]
    if missing:
        console.warn(f"missing required argument(s): {', '.join('--' + m for m in missing)}")
        return 2

    repo = default_repo() if args.repo == "<origin>" else (args.repo or None)
    rw = Railway(console, dry_run=args.dry_run)

    # Railway's link state is per-directory, and `railway volume add` acts on the linked service.
    os.chdir(REPO_ROOT)

    try:
        if args.dry_run:
            console.say("DRY RUN — reads are real, mutations are only printed.")
        preflight(rw, console, args.workspace)
        ensure_project(
            rw, console, name=args.project, workspace=args.workspace, env=args.environment
        )
        postgres = ensure_postgres(rw, console)
        ensure_gowa(rw, console, postgres_service=postgres)
        ensure_penny(rw, console, domain=args.domain, postgres_service=postgres)
        ensure_volume(rw, console)

        console.head("Variables")
        penny_changed = apply_variables(
            rw,
            console,
            PENNY_SERVICE,
            penny_vars(domain=args.domain, postgres_service=postgres),
        )
        gowa_changed = apply_variables(
            rw, console, GOWA_SERVICE, gowa_vars(postgres_service=postgres)
        )

        console.head("Secrets")
        penny_live = live_variables(rw, PENNY_SERVICE)
        gowa_live = live_variables(rw, GOWA_SERVICE)
        penny_secrets, gowa_secrets, minted = reconcile_secrets(
            rw,
            console,
            penny_live=penny_live,
            gowa_live=gowa_live,
            openai_key=os.environ.get("OPENAI_API_KEY"),
        )
        for var in penny_secrets:
            set_variable(rw, PENNY_SERVICE, var)
        for var in gowa_secrets:
            set_variable(rw, GOWA_SERVICE, var)
        penny_changed = penny_changed or bool(penny_secrets)
        gowa_changed = gowa_changed or bool(gowa_secrets)

        if args.prune:
            console.head("Prune (variables that only restate a code default)")
            prune(rw, console, PENNY_SERVICE, penny_live)
            prune(rw, console, GOWA_SERVICE, gowa_live)
            penny_changed = penny_changed or bool(plan_prune(penny_live))
            gowa_changed = gowa_changed or bool(plan_prune(gowa_live))

        ensure_source(rw, console, repo=repo, branch=args.branch)
        dns = ensure_domain(rw, console, domain=args.domain)

        console.head("Deploy")
        if args.no_deploy:
            console.step("skipped (--no-deploy)")
        else:
            # Every variable above was written with --skip-deploys, so exactly one deploy per
            # service happens here instead of one per variable.
            if repo:
                rw.write("redeploy", "--service", PENNY_SERVICE, "--yes", "--from-source", "--json")
            else:
                rw.write("up", "--service", PENNY_SERVICE, "--detach")
            if gowa_changed:
                rw.write("redeploy", "--service", GOWA_SERVICE, "--yes", "--json")
            else:
                console.step(f"{GOWA_SERVICE}: unchanged, not redeployed")

        report_generated_secrets(console, minted)
        report_manual_steps(console, domain=args.domain, dns=dns)
        console.say("\nDone. Runbook: docs/deployment.md")
        return 0

    except Failure as exc:
        console.warn(str(exc))
        return 1
    except KeyboardInterrupt:
        console.warn("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
