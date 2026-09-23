#!/usr/bin/env python3
"""
fetch.py, Microsoft Graph fetcher for the ingest-outlook skill.

Author:   Danny Bravo
License:  MIT
Project:  https://github.com/danilobrando/ingest-outlook

Subcommands:
  mail      Outlook messages from a folder or free-text search query
  calendar  Calendar events from /me/calendarView
  meetings  Teams online meetings + transcripts (corporate accounts only)

Auth:
  OAuth 2.0 authorization-code flow with PKCE (RFC 7636), loopback redirect
  on http://localhost:<INGEST_OUTLOOK_REDIRECT_PORT>/callback (default 8765).
  No client secret required.

Config (`fetch.py configure` or env vars):
  MS_GRAPH_CLIENT_ID            Azure app Application (client) ID
  MS_GRAPH_TENANT_ID            (optional, default "common") Azure tenant:
                                  - "common": personal + work/school accounts
                                  - "consumers": personal MS accounts only
                                  - "<tenant-guid>": single-tenant corporate
  MS_GRAPH_SCOPES               (optional) Explicit space-separated scopes
  INGEST_OUTLOOK_READ_ONLY      (optional) Enable the read-only profile
  INGEST_OUTLOOK_VAULT_ROOT     (optional) Default vault path
  INGEST_OUTLOOK_OUTPUT_DIR     (optional) Relative output prefix in vault
  INGEST_OUTLOOK_REDIRECT_PORT  (optional, default 8765) OAuth loopback port

Output: JSON payload on stdout in the shape ingest.py expects.

State defaults to ~/.config/ingest-outlook (POSIX mode 0600 files; Windows ACL).

Stdlib only. No external dependencies.
"""
from __future__ import annotations

__author__ = "Danny Bravo"
__version__ = "0.5.0"
__license__ = "MIT"

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Constants and config
# ---------------------------------------------------------------------------

DEFAULT_AUTHORITY_BASE = "https://login.microsoftonline.com"
DEFAULT_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
AUTHORITY_BASE = os.environ.get("INGEST_OUTLOOK_AUTHORITY_BASE", DEFAULT_AUTHORITY_BASE).rstrip("/")
GRAPH_BASE = os.environ.get("INGEST_OUTLOOK_GRAPH_BASE", DEFAULT_GRAPH_BASE).rstrip("/")

DEFAULT_PERSONAL_SCOPES = [
    "User.Read",
    "Mail.Read",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Calendars.Read.Shared",
    "offline_access",
]
DEFAULT_CORPORATE_SCOPES = [
    "User.Read",
    "Mail.Read",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Calendars.Read.Shared",
    "OnlineMeetings.Read",
    "OnlineMeetingTranscript.Read.All",
    "offline_access",
]

DEFAULT_READ_ONLY_SCOPES = [
    "User.Read",
    "Mail.Read",
    "Calendars.Read",
    "offline_access",
]
DEFAULT_OUTPUT_DIR = "External Inputs/Outlook"
EXIT_READ_ONLY_BLOCKED = 3
# Entra often omits these from the token `scope` even when consent succeeded.
# Treating them as missing makes `doctor`/`fix` re-authenticate forever.
PROTOCOL_SCOPES = frozenset({"offline_access", "openid", "profile", "email"})
GRAPH_SCOPE_PREFIX = "https://graph.microsoft.com/"

TOKEN_DIR = Path(
    os.environ.get(
        "INGEST_OUTLOOK_CONFIG_DIR",
        str(Path.home() / ".config" / "ingest-outlook"),
    )
).expanduser()
CONFIG_PATH = TOKEN_DIR / "config.json"
TOKEN_PATH = TOKEN_DIR / "token.json"
LOG_PATH = TOKEN_DIR / "log.jsonl"
AUDIT_PATH = TOKEN_DIR / "audit.jsonl"
LOCK_PATH = TOKEN_DIR / "refresh.lock"

MESSAGE_LIMIT = 50            # max items per Graph page
MAX_PAGES = 4                 # cap on pagination (default 200 items max per fetch)
HTTP_TIMEOUT = 30
MAX_RETRIES = 3
TIME_SKEW_WARN_SECONDS = 60
TOKEN_FRESHNESS_BUFFER = 300  # require >5 min remaining; force prophylactic refresh otherwise
LOCK_TIMEOUT = 10             # seconds to wait for refresh lock
LOCK_STALE_AGE = 60           # seconds before a held lock is considered stale and stolen

# Process-level state (reset per CLI invocation)
_time_skew_checked = False
_run_started_at: float | None = None  # set in main()
_active_cfg: "Config | None" = None   # set by get_access_token; used by mid-run 401 retry
_verbose_mode = False                 # set by --verbose flag
_quiet_mode = False                   # set by --quiet flag

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
TENANT_ALIASES = {"common", "organizations", "consumers"}
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _configure_console_encoding() -> None:
    """Emit UTF-8 diagnostics even on legacy Windows code pages (cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def _chmod_private(path: Path, mode: int) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _ensure_config_dir() -> None:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _chmod_private(TOKEN_DIR, 0o700)


def _command_hint(command: str = "fix") -> str:
    return f'"{sys.executable}" "{Path(__file__).resolve()}" {command}'


def _verbose(msg: str) -> None:
    """Print to stderr only if --verbose is on. Use for debugging hints
    (URLs being hit, scopes, token expiry, retry attempts)."""
    if _verbose_mode and not _quiet_mode:
        print(f"  [verbose] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# AADSTS error catalogue. When Microsoft's token endpoint or Graph throws
# one of these, we look up the code and surface a human-readable message
# plus a recommended next action. Generic catch-all at the end.
# ---------------------------------------------------------------------------

AADSTS_HANDLERS: dict[str, tuple[str, str]] = {
    "AADSTS50158": (
        "Conditional Access policy requires interactive sign-in.",
        "Run any command to trigger a fresh browser auth. If MFA is required, complete it in the browser.",
    ),
    "AADSTS50173": (
        "Fresh authentication required (token too old, policy reset, or password changed).",
        f"Delete the token cache at {TOKEN_PATH} and re-authenticate with: {_command_hint('fix')}",
    ),
    "AADSTS70008": (
        "Refresh token expired or revoked (90-day max for personal, tenant policy for corporate).",
        f"Delete the token cache at {TOKEN_PATH} and re-authenticate with: {_command_hint('fix')}",
    ),
    "AADSTS65001": (
        "User or admin has not consented to one or more requested scopes.",
        "Re-authenticate and grant the requested permissions. If a scope requires admin consent, contact your Azure admin.",
    ),
    "AADSTS90094": (
        "Admin consent required for one or more scopes (e.g. OnlineMeetingTranscript.Read.All).",
        "Ask your Azure admin to click 'Grant admin consent' in the app's API permissions page.",
    ),
    "AADSTS50076": (
        "Multi-factor authentication required.",
        "Complete MFA in the browser when re-authenticating.",
    ),
    "AADSTS50034": (
        "User account does not exist in the tenant you targeted.",
        "Verify MS_GRAPH_TENANT_ID matches the tenant where the account lives.",
    ),
    "AADSTS900971": (
        "User account is locked out.",
        "Contact your IT admin to unlock the account.",
    ),
    "AADSTS700016": (
        "Application not found in the directory (wrong client ID for this tenant).",
        "Verify MS_GRAPH_CLIENT_ID matches the app registered in this tenant.",
    ),
    "AADSTS500011": (
        "App not provisioned in the tenant (multi-tenant app needs initial admin consent).",
        "Ask your Azure admin to provision the app in this tenant before first use.",
    ),
    "AADSTS500113": (
        "No reply address registered for the application.",
        "Verify the app's Authentication blade has http://localhost:8765/callback as a redirect URI for 'Mobile and desktop applications'.",
    ),
    "AADSTS50011": (
        "The redirect URI does not match the app registration.",
        "Configure {redirect_uri} under the app's 'Mobile and desktop applications' platform.",
    ),
    "AADSTS7000218": (
        "The app is not configured as a public client and Microsoft expected a client secret.",
        "Set 'Allow public client flows' to Yes. This PKCE desktop app must not use a client secret.",
    ),
    "AADSTS9002313": (
        "Invalid request (malformed parameter).",
        "Re-run with --verbose and inspect the URL. May indicate a code bug; file an issue with the trace ID.",
    ),
}


def explain_aadsts(error_body: str, redirect_uri: str | None = None) -> str | None:
    """Scan an error body for a known AADSTS code. Returns a multi-line
    human-readable explanation, or None if no known code matches.

    AADSTS50011 names the redirect URI from the active config (the port is
    configurable). Callers that have a Config should pass redirect_uri.
    """
    uri = redirect_uri or "http://localhost:8765/callback"
    for code, (desc, fix) in AADSTS_HANDLERS.items():
        if code in error_body:
            # AADSTS500113 contains the substring AADSTS50011; the catalogue
            # lists 500113 first so the longer code wins.
            if "{redirect_uri}" in fix:
                fix = fix.format(redirect_uri=uri)
            return f"\n  Error code:  {code}\n  Meaning:     {desc}\n  Next action: {fix}"
    return None


# ---------------------------------------------------------------------------
# Logging (operational) and audit (write actions only)
# ---------------------------------------------------------------------------

def _now_local_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _log_event(
    command: str,
    status: str,
    duration_ms: int | None = None,
    error_code: str | None = None,
    result_count: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one JSON line to log.jsonl. Best-effort: any IO error is
    swallowed so logging never breaks the main operation.
    """
    try:
        _ensure_config_dir()
        event: dict[str, Any] = {
            "ts": _now_local_iso(),
            "command": command,
            "status": status,
            "version": __version__,
        }
        if duration_ms is not None:
            event["duration_ms"] = duration_ms
        if error_code is not None:
            event["error_code"] = error_code
        if result_count is not None:
            event["result_count"] = result_count
        if extra:
            event.update(extra)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        _chmod_private(LOG_PATH, 0o600)
    except OSError:
        pass


def _audit_action(
    action: str,
    status: str,
    summary: dict[str, Any],
    result_id: str | None = None,
    error_code: str | None = None,
) -> None:
    """Append one JSON line to audit.jsonl. Captures every write action so
    the user can audit what the connector did in their name. Best-effort like
    _log_event.

    The summary should include recipient counts/addresses, subject, body
    char count, and a sha8 of the body for forensic correlation (we do NOT
    log full body content to avoid PII expansion in plaintext).
    """
    try:
        _ensure_config_dir()
        event: dict[str, Any] = {
            "ts": _now_local_iso(),
            "action": action,
            "status": status,
            "version": __version__,
            "summary": summary,
        }
        if result_id is not None:
            event["result_id"] = result_id
        if error_code is not None:
            event["error_code"] = error_code
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        _chmod_private(AUDIT_PATH, 0o600)
    except OSError:
        pass


def _body_sha8(body: str) -> str:
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:8]


# (Pagination warnings are emitted directly from _paginate() now;
# the older _warn_if_capped() helper is no longer needed.)


class Config:
    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        scopes: list[str],
        redirect_port: int,
        read_only: bool = False,
        teams: bool = False,
        shared_calendars: bool = False,
        output_dir: str = DEFAULT_OUTPUT_DIR,
        vault_root: str | None = None,
        graph_base: str = DEFAULT_GRAPH_BASE,
        authority_base: str = DEFAULT_AUTHORITY_BASE,
        sources: dict[str, str] | None = None,
    ):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.scopes = scopes
        self.redirect_port = redirect_port
        self.read_only = read_only
        self.teams = teams
        self.shared_calendars = shared_calendars
        self.output_dir = output_dir
        self.vault_root = vault_root
        self.graph_base = graph_base.rstrip("/")
        self.authority_base = authority_base.rstrip("/")
        self.sources = sources or {}

    @property
    def authority(self) -> str:
        return f"{self.authority_base}/{self.tenant_id}"

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.redirect_port}/callback"

    @property
    def is_personal_tenant(self) -> bool:
        return self.tenant_id in ("common", "consumers")


# Set when config.json exists but cannot be used. Doctor reports it as FAIL.
_config_file_error: str | None = None


def _read_config_file() -> dict[str, Any]:
    """Load config.json. A missing file is empty config, not an error.

    UTF-8 BOM (Notepad on Windows) is accepted. Invalid JSON or a non-object
    is reported on stderr and treated as empty so the process can continue;
    it is not a silent {}.
    """
    global _config_file_error
    _config_file_error = None
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        _config_file_error = str(exc)
        print(
            f"config.json inválido en {CONFIG_PATH}: {exc}; "
            "corre `configure` para regenerarlo",
            file=sys.stderr,
        )
        return {}
    if not isinstance(data, dict):
        _config_file_error = (
            f"se esperaba un objeto JSON, se recibió {type(data).__name__}"
        )
        print(
            f"config.json inválido en {CONFIG_PATH}: {_config_file_error}; "
            "corre `configure` para regenerarlo",
            file=sys.stderr,
        )
        return {}
    return data


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def validate_output_dir(value: str) -> str:
    value = str(value).strip().replace("\\", "/")
    posix_path = Path(value)
    windows_path = PureWindowsPath(value)
    if not value or posix_path.is_absolute() or windows_path.is_absolute():
        raise ValueError("output_dir must be a non-empty path relative to the vault")
    if any(part in {"", ".", ".."} for part in posix_path.parts):
        raise ValueError("output_dir must not contain '.' or '..' components")
    for part in posix_path.parts:
        if re.search(r'[:*?"<>|]', part) or part.endswith((".", " ")):
            raise ValueError(f"output_dir contains a Windows-unsafe component: {part!r}")
        if part.lower() in WINDOWS_RESERVED_NAMES:
            raise ValueError(f"output_dir contains a Windows reserved name: {part!r}")
    return value.rstrip("/")


def _value_with_source(
    key: str,
    arg_value: Any,
    env_name: str | None,
    file_config: dict[str, Any],
    default: Any,
) -> tuple[Any, str]:
    if arg_value is not None:
        return arg_value, "arg"
    if env_name and env_name in os.environ:
        return os.environ[env_name], "env"
    if key in file_config and file_config[key] is not None:
        return file_config[key], "file"
    return default, "default"


def _print_missing_client_id() -> None:
    if os.name == "nt":
        setup = f"  {_command_hint('configure')} --client-id <guid> --tenant-id <guid>"
    else:
        setup = (
            f"  {_command_hint('configure')} --client-id <guid> --tenant-id <guid>\n"
            "or export MS_GRAPH_CLIENT_ID in ~/.zshrc (or your shell profile)."
        )
    print(
        "ERROR: Microsoft Graph client ID is not configured. / "
        "El client ID de Microsoft Graph no está configurado.\n"
        "Register an Entra app and configure its Application (client) ID.\n"
        f"{setup}\n\nFor step-by-step recovery, run:\n  {_command_hint('fix')}",
        file=sys.stderr,
    )


def load_config(
    client_id_arg: str | None = None,
    vault_root_arg: str | None = None,
    overrides: dict[str, Any] | None = None,
    require_client: bool = True,
) -> Config:
    """Load effective config with arg > env > config.json > default precedence."""
    global GRAPH_BASE, AUTHORITY_BASE
    file_config = _read_config_file()
    args = dict(overrides or {})
    if client_id_arg is not None:
        args["client_id"] = client_id_arg
    if vault_root_arg is not None:
        args["vault_root"] = vault_root_arg

    specs = {
        "client_id": ("MS_GRAPH_CLIENT_ID", ""),
        "tenant_id": ("MS_GRAPH_TENANT_ID", "common"),
        "redirect_port": ("INGEST_OUTLOOK_REDIRECT_PORT", 8765),
        "read_only": ("INGEST_OUTLOOK_READ_ONLY", False),
        "output_dir": ("INGEST_OUTLOOK_OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
        "vault_root": ("INGEST_OUTLOOK_VAULT_ROOT", None),
        "teams": (None, False),
        "shared_calendars": (None, False),
    }
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, (env_name, default) in specs.items():
        values[key], sources[key] = _value_with_source(
            key, args.get(key), env_name, file_config, default
        )

    client_id = str(values["client_id"] or "").strip()
    tenant_id = str(values["tenant_id"] or "common").strip()
    try:
        port = int(values["redirect_port"])
    except (TypeError, ValueError) as exc:
        raise ValueError("INGEST_OUTLOOK_REDIRECT_PORT must be an integer") from exc
    if not (1 <= port <= 65535):
        raise ValueError("redirect_port must be between 1 and 65535")
    read_only = _parse_bool(values["read_only"])
    teams = _parse_bool(values["teams"])
    shared_calendars = _parse_bool(values["shared_calendars"])
    output_dir = validate_output_dir(str(values["output_dir"]))
    vault_root = str(values["vault_root"]).strip() if values["vault_root"] else None

    scopes_env = os.environ.get("MS_GRAPH_SCOPES", "").strip()
    if scopes_env:
        scopes = scopes_env.split()
        sources["scopes"] = "env"
        if read_only:
            scopes, discarded = _filter_read_only_scopes(scopes)
            if discarded:
                # MS_GRAPH_SCOPES must not reopen write access under read-only.
                sources["scopes"] = "env (filtered: read-only)"
                listed = ", ".join(discarded)
                print(
                    "WARNING: read-only profile discarded write scopes from "
                    f"MS_GRAPH_SCOPES: {listed}. / "
                    "ADVERTENCIA: el perfil de solo lectura descartó scopes de "
                    f"escritura de MS_GRAPH_SCOPES: {listed}.",
                    file=sys.stderr,
                )
    elif read_only:
        scopes = list(DEFAULT_READ_ONLY_SCOPES)
        if shared_calendars:
            scopes.append("Calendars.Read.Shared")
        if teams:
            scopes.extend(["OnlineMeetings.Read", "OnlineMeetingTranscript.Read.All"])
        sources["scopes"] = "default(read-only profile)"
    elif tenant_id in ("common", "consumers"):
        scopes = list(DEFAULT_PERSONAL_SCOPES)
        sources["scopes"] = "default(personal profile)"
    else:
        scopes = list(DEFAULT_CORPORATE_SCOPES)
        sources["scopes"] = "default(corporate profile)"

    if require_client and not client_id:
        _print_missing_client_id()
        raise SystemExit(2)

    graph_base = os.environ.get("INGEST_OUTLOOK_GRAPH_BASE", DEFAULT_GRAPH_BASE)
    authority_base = os.environ.get("INGEST_OUTLOOK_AUTHORITY_BASE", DEFAULT_AUTHORITY_BASE)
    sources["graph_base"] = "env" if "INGEST_OUTLOOK_GRAPH_BASE" in os.environ else "default"
    sources["authority_base"] = "env" if "INGEST_OUTLOOK_AUTHORITY_BASE" in os.environ else "default"
    cfg = Config(
        client_id=client_id,
        tenant_id=tenant_id,
        scopes=scopes,
        redirect_port=port,
        read_only=read_only,
        teams=teams,
        shared_calendars=shared_calendars,
        output_dir=output_dir,
        vault_root=vault_root,
        graph_base=graph_base,
        authority_base=authority_base,
        sources=sources,
    )
    GRAPH_BASE = cfg.graph_base
    AUTHORITY_BASE = cfg.authority_base
    return cfg


# ---------------------------------------------------------------------------
# Token store
# ---------------------------------------------------------------------------

def _load_token() -> dict[str, Any] | None:
    if not TOKEN_PATH.is_file():
        return None
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_token(token: dict[str, Any]) -> None:
    # Capture the scope set Microsoft actually granted (may be narrower than
    # what we requested). Persisting this lets `doctor` flag scope drift.
    scope_str = token.get("scope", "")
    if scope_str:
        token["scopes_granted"] = scope_str.split()
    _ensure_config_dir()
    TOKEN_PATH.write_text(json.dumps(token, indent=2), encoding="utf-8")
    _chmod_private(TOKEN_PATH, 0o600)


def _normalize_scope(scope: str) -> str:
    """Strip a Graph resource prefix and surrounding whitespace.

    Entra sometimes returns `https://graph.microsoft.com/Mail.Read` instead of
    the short scope name we requested.
    """
    text = str(scope).strip()
    if text.lower().startswith(GRAPH_SCOPE_PREFIX):
        text = text[len(GRAPH_SCOPE_PREFIX):]
    return text.strip()


def _is_protocol_scope(scope: str) -> bool:
    return _normalize_scope(scope).lower() in PROTOCOL_SCOPES


def _scope_is_write(scope: str) -> bool:
    """True for Graph write scopes. `.Read.All` is read-only and must stay."""
    lowered = _normalize_scope(scope).lower()
    return "readwrite" in lowered or ".send" in lowered


def _filter_read_only_scopes(scopes: list[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    discarded: list[str] = []
    for scope in scopes:
        if _scope_is_write(scope):
            discarded.append(scope)
        else:
            kept.append(scope)
    return kept, discarded


def verify_scopes(token: dict[str, Any], required: list[str]) -> tuple[bool, list[str]]:
    """Compare the token's `scopes_granted` against a required list. Returns
    (all_present, missing_scopes). If the token has no scopes_granted field
    (legacy token from before we tracked this), returns (True, []) and
    lets the operation proceed.

    Comparison is case-insensitive. Granted scopes may use the
    `https://graph.microsoft.com/` prefix. `offline_access`, `openid`,
    `profile`, and `email` are protocol scopes: Entra may omit them from the
    token response even after consent, so they are not treated as missing.
    """
    raw_granted = token.get("scopes_granted") or []
    if not raw_granted:
        return True, []
    granted = {
        _normalize_scope(scope).lower()
        for scope in raw_granted
        if not _is_protocol_scope(str(scope))
    }
    missing = [
        scope for scope in required
        if not _is_protocol_scope(str(scope))
        and _normalize_scope(str(scope)).lower() not in granted
    ]
    return not missing, missing


def _delete_token() -> None:
    try:
        TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass


def _token_is_fresh(token: dict[str, Any]) -> bool:
    """A token is 'fresh' if it has more than TOKEN_FRESHNESS_BUFFER seconds
    left. The buffer is generous (300s) so multi-step operations like
    `meetings` (which can take minutes across many Graph calls) don't hit
    mid-run expiry; we proactively refresh before starting.
    """
    expires_at = token.get("expires_at")
    if not expires_at:
        return False
    return time.time() < (expires_at - TOKEN_FRESHNESS_BUFFER)


# ---------------------------------------------------------------------------
# Refresh lock (best-effort, prevents two processes from refreshing at once
# and corrupting each other's token cache)
# ---------------------------------------------------------------------------

def _acquire_refresh_lock(timeout: int = LOCK_TIMEOUT) -> bool:
    """Best-effort exclusive lock via O_CREAT|O_EXCL. Returns True if
    acquired. Treats locks older than LOCK_STALE_AGE as orphans and steals
    them (a previous process likely crashed mid-refresh).
    """
    _ensure_config_dir()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - LOCK_PATH.stat().st_mtime
                if age > LOCK_STALE_AGE:
                    _verbose(f"stealing stale refresh lock ({int(age)}s old)")
                    LOCK_PATH.unlink()
                    continue
            except OSError:
                pass
            time.sleep(0.1)
    return False


def _release_refresh_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# OAuth2 PKCE
# ---------------------------------------------------------------------------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _generate_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    captured: dict[str, str] = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        for k in ("code", "state", "error", "error_description"):
            v = params.get(k)
            if v:
                _CallbackHandler.captured[k] = v[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            b"<html><body style='font-family:system-ui;padding:40px'>"
            b"<h2>ingest-outlook</h2>"
            b"<p>Authorization captured. You can close this tab.</p>"
            b"</body></html>"
        )
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002
        return


def _run_oauth_flow(config: Config) -> dict[str, Any]:
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(24)

    auth_params = {
        "client_id": config.client_id,
        "response_type": "code",
        "redirect_uri": config.redirect_uri,
        "response_mode": "query",
        "scope": " ".join(config.scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    auth_url = f"{config.authority}/oauth2/v2.0/authorize?{urllib.parse.urlencode(auth_params)}"

    _CallbackHandler.captured = {}
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(
            ("127.0.0.1", config.redirect_port), _CallbackHandler
        )
    except OSError as e:
        print(
            f"ERROR: cannot bind to localhost:{config.redirect_port} for OAuth callback: {e}\n"
            f"If another process is on that port, stop it and retry. The redirect URI\n"
            f"registered in Azure must match {config.redirect_uri}.",
            file=sys.stderr,
        )
        sys.exit(2)

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    print("Opening browser to authorize Microsoft Graph access...", file=sys.stderr)
    print(f"If the browser does not open, visit:\n  {auth_url}", file=sys.stderr)
    webbrowser.open(auth_url)

    deadline = time.time() + 300
    while time.time() < deadline:
        if "code" in _CallbackHandler.captured or "error" in _CallbackHandler.captured:
            break
        time.sleep(0.25)

    httpd.shutdown()
    httpd.server_close()

    if "error" in _CallbackHandler.captured:
        err = _CallbackHandler.captured.get("error")
        desc = _CallbackHandler.captured.get("error_description", "")
        print(f"ERROR: OAuth authorization failed: {err}: {desc}", file=sys.stderr)
        sys.exit(2)

    if "code" not in _CallbackHandler.captured:
        print("ERROR: OAuth flow timed out before receiving a code.", file=sys.stderr)
        sys.exit(2)

    if _CallbackHandler.captured.get("state") != state:
        print("ERROR: OAuth state mismatch (possible CSRF). Aborting.", file=sys.stderr)
        sys.exit(2)

    code = _CallbackHandler.captured["code"]
    return _exchange_code_for_token(config, code, verifier)


def _exchange_code_for_token(config: Config, code: str, verifier: str) -> dict[str, Any]:
    data = urllib.parse.urlencode({
        "client_id": config.client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": config.redirect_uri,
        "code_verifier": verifier,
        "scope": " ".join(config.scopes),
    }).encode("ascii")
    return _post_token(config, data)


def _refresh_token(config: Config, refresh_token: str) -> dict[str, Any]:
    data = urllib.parse.urlencode({
        "client_id": config.client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": " ".join(config.scopes),
    }).encode("ascii")
    return _post_token(config, data)


def _post_token(config: Config, data: bytes) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{config.authority}/oauth2/v2.0/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        explanation = explain_aadsts(body, redirect_uri=config.redirect_uri)
        print(f"ERROR: token endpoint returned HTTP {e.code}.", file=sys.stderr)
        if explanation:
            print(explanation, file=sys.stderr)
            # Pull the trace id from the body for support tickets
            import re as _re
            m = _re.search(r"Trace ID: ([0-9a-f-]+)", body)
            if m:
                print(f"  Trace ID:    {m.group(1)}", file=sys.stderr)
        else:
            # Unknown AADSTS code; show first 600 chars verbatim
            print(f"  Body excerpt: {body[:600]}", file=sys.stderr)
        _log_event("token_endpoint", "failure", error_code=f"http_{e.code}", extra={"explained": bool(explanation)})
        raise
    payload["expires_at"] = int(time.time()) + int(payload.get("expires_in", 0))
    return payload


def get_access_token(config: Config, force_refresh: bool = False) -> str:
    """Return a valid access_token. Refreshes via cached refresh_token when
    near expiry, or runs full OAuth if refresh fails. Wrapped in a file lock
    so concurrent invocations don't fight over the same token cache.

    Side effect: records `config` as the module-level active cfg so
    mid-run 401 retries can refresh without re-passing the cfg around.
    """
    global _active_cfg
    _active_cfg = config

    token = _load_token()
    if not force_refresh and token and _token_is_fresh(token):
        return token["access_token"]

    got_lock = _acquire_refresh_lock()
    if not got_lock:
        # Another process is refreshing. Wait a moment and try the cache again;
        # if still stale, proceed without the lock (best-effort).
        time.sleep(2)
        token = _load_token()
        if token and _token_is_fresh(token):
            _verbose("another process refreshed while we waited; using fresh token")
            return token["access_token"]
        _verbose("could not acquire refresh lock; proceeding anyway")

    try:
        if token and token.get("refresh_token"):
            try:
                _verbose("refreshing token via refresh_token grant")
                refreshed = _refresh_token(config, token["refresh_token"])
                if not refreshed.get("refresh_token"):
                    refreshed["refresh_token"] = token["refresh_token"]
                _save_token(refreshed)
                return refreshed["access_token"]
            except urllib.error.HTTPError:
                print(
                    "Cached refresh token rejected. Starting a fresh OAuth flow.",
                    file=sys.stderr,
                )
                _delete_token()

        _verbose("running full OAuth authorization-code flow")
        token = _run_oauth_flow(config)
        _save_token(token)
        return token["access_token"]
    finally:
        if got_lock:
            _release_refresh_lock()


# ---------------------------------------------------------------------------
# Microsoft Graph HTTP
# ---------------------------------------------------------------------------

def _graph_request(
    url: str,
    access_token: str,
    accept: str = "application/json",
    prefer: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    last_err: Exception | None = None
    refreshed_in_loop = False
    for attempt in range(MAX_RETRIES):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": accept,
        }
        if prefer:
            headers["Prefer"] = prefer
        _verbose(f"GET {url}")
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                _check_time_skew_once(dict(resp.headers))
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "2"))
                _verbose(f"429 throttled; sleeping {min(retry_after, 30)}s")
                time.sleep(min(retry_after, 30))
                last_err = e
                continue
            if e.code == 401 and not refreshed_in_loop and _active_cfg is not None:
                # Token rejected mid-run. Force a refresh and retry once.
                _verbose("401 on GET; forcing token refresh and retrying once")
                try:
                    access_token = get_access_token(_active_cfg, force_refresh=True)
                    refreshed_in_loop = True
                    continue
                except Exception as refresh_err:
                    _verbose(f"mid-run refresh failed: {refresh_err}")
            raise
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(2 ** attempt)
    if last_err:
        raise last_err
    raise RuntimeError("graph request exhausted retries without an error object")


def _check_time_skew_once(headers: dict[str, str]) -> None:
    """Compare the Date header from Graph against local clock. Warn once per
    process if skew exceeds TIME_SKEW_WARN_SECONDS. Clock skew is the root
    cause of many OAuth failures (token nbf/exp claims) and is easy to miss.
    """
    global _time_skew_checked
    if _time_skew_checked:
        return
    _time_skew_checked = True
    date_header = headers.get("Date") or headers.get("date")
    if not date_header:
        return
    try:
        from email.utils import parsedate_to_datetime
        server_dt = parsedate_to_datetime(date_header)
        local_dt = datetime.now(timezone.utc)
        skew = abs((local_dt - server_dt).total_seconds())
        if skew > TIME_SKEW_WARN_SECONDS:
            msg = (
                f"WARNING: clock skew {int(skew)}s vs Microsoft Graph. "
                f"Tokens may be rejected due to nbf/exp claims. Sync your "
                f"system clock (System Settings > Date & Time on macOS)."
            )
            print(msg, file=sys.stderr)
            _log_event("time_skew", "warning", extra={"skew_seconds": int(skew)})
    except Exception:
        pass


def _graph_get_json(url: str, access_token: str, prefer: str | None = None) -> dict[str, Any]:
    try:
        _, body, _ = _graph_request(url, access_token, prefer=prefer)
        return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: Graph {e.code} on {url}: {body[:400]}", file=sys.stderr)
        raise


def _graph_write(
    method: str,
    url: str,
    access_token: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST/PATCH/DELETE to Graph with a JSON body. Returns parsed response
    dict (empty for 204 No Content). Honors 429 throttling and auto-refreshes
    on 401 (mid-run token expiry).
    """
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    last_err: Exception | None = None
    refreshed_in_loop = False
    for attempt in range(MAX_RETRIES):
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        _verbose(f"{method} {url}")
        req = urllib.request.Request(url, data=payload, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "2"))
                _verbose(f"429 throttled; sleeping {min(retry_after, 30)}s")
                time.sleep(min(retry_after, 30))
                last_err = e
                continue
            if e.code == 401 and not refreshed_in_loop and _active_cfg is not None:
                _verbose(f"401 on {method}; forcing token refresh and retrying once")
                try:
                    access_token = get_access_token(_active_cfg, force_refresh=True)
                    refreshed_in_loop = True
                    continue
                except Exception as refresh_err:
                    _verbose(f"mid-run refresh failed: {refresh_err}")
            err_body = e.read().decode("utf-8", errors="replace")
            print(f"ERROR: Graph {e.code} on {method} {url}: {err_body[:600]}", file=sys.stderr)
            raise
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(2 ** attempt)
    if last_err:
        raise last_err
    raise RuntimeError("graph write exhausted retries without an error object")


def _graph_get_text(url: str, access_token: str, accept: str) -> str:
    try:
        _, body, _ = _graph_request(url, access_token, accept=accept)
        return body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"ERROR: Graph {e.code} on {url}: {err_body[:400]}", file=sys.stderr)
        raise


# ---------------------------------------------------------------------------
# URL builder for OData queries
# ---------------------------------------------------------------------------

def _odata_url(base: str, params: dict[str, Any]) -> str:
    """Build a Graph URL with OData params. Encodes spaces (%20), preserves /,:
    so that ISO timestamps, $orderby property paths, and $select field lists
    stay readable. Python 3.12 urllib refuses URLs with raw control chars
    (including spaces), so $filter=displayName eq 'X' cannot be passed raw.
    """
    parts: list[str] = []
    for key, value in params.items():
        encoded = urllib.parse.quote(str(value), safe="/,:")
        parts.append(f"{key}={encoded}")
    return f"{base}?{'&'.join(parts)}"


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------

def resolve_folder_id(folder_name: str, access_token: str) -> str | None:
    safe_name = folder_name.replace("'", "''")
    url = _odata_url(f"{GRAPH_BASE}/me/mailFolders", {
        "$filter": f"displayName eq '{safe_name}'",
        "$top": "10",
    })
    data = _graph_get_json(url, access_token)
    folders = data.get("value", [])
    if not folders:
        return None
    return folders[0].get("id")


def list_folders(access_token: str) -> list[str]:
    url = f"{GRAPH_BASE}/me/mailFolders?$top=50&$select=displayName"
    data = _graph_get_json(url, access_token)
    return [f.get("displayName", "") for f in data.get("value", []) if f.get("displayName")]


def _paginate(
    initial_url: str,
    access_token: str,
    prefer: str | None,
    max_pages: int,
    context: str,
) -> list[dict[str, Any]]:
    """Follow Microsoft Graph @odata.nextLink up to max_pages. Returns the
    concatenated list of items. Logs a warning if the cap was reached and
    more results were available (so the operator knows to narrow filters).
    """
    all_values: list[dict[str, Any]] = []
    url: str | None = initial_url
    pages = 0
    while url and pages < max_pages:
        data = _graph_get_json(url, access_token, prefer=prefer)
        page_values = data.get("value", [])
        all_values.extend(page_values)
        url = data.get("@odata.nextLink")
        pages += 1
        _verbose(f"{context} page {pages}: {len(page_values)} items (running total {len(all_values)})")
    if url:
        msg = (
            f"WARNING: {context} hit pagination cap ({max_pages} pages, {len(all_values)} items); "
            f"more results exist beyond this window. Narrow filters or raise MAX_PAGES."
        )
        print(msg, file=sys.stderr)
        _log_event(context, "warning", extra={"reason": "pagination_cap_reached", "items_returned": len(all_values)})
    return all_values


def fetch_folder_messages(
    folder_id: str,
    since_iso: str,
    access_token: str,
    limit: int = MESSAGE_LIMIT,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    select = ",".join([
        "id", "subject", "from", "toRecipients", "ccRecipients",
        "receivedDateTime", "body", "bodyPreview", "categories",
    ])
    url = _odata_url(
        f"{GRAPH_BASE}/me/mailFolders/{folder_id}/messages",
        {
            "$top": str(limit),
            "$filter": f"receivedDateTime ge {since_iso}",
            "$orderby": "receivedDateTime desc",
            "$select": select,
        },
    )
    return _paginate(url, access_token, prefer='outlook.body-content-type="text"',
                     max_pages=max_pages, context="mail folder fetch")


def search_messages(
    query: str,
    access_token: str,
    limit: int = MESSAGE_LIMIT,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    select = ",".join([
        "id", "subject", "from", "toRecipients", "ccRecipients",
        "receivedDateTime", "body", "bodyPreview", "categories",
    ])
    quoted = '"' + query.replace('"', '\\"') + '"'
    url = _odata_url(f"{GRAPH_BASE}/me/messages", {
        "$top": str(limit),
        "$search": quoted,
        "$select": select,
    })
    return _paginate(url, access_token, prefer='outlook.body-content-type="text"',
                     max_pages=max_pages, context="mail search")


def _addr_to_str(addr: dict[str, Any] | None) -> str:
    if not addr:
        return ""
    email = (addr.get("emailAddress") or {}).get("address") or ""
    name = (addr.get("emailAddress") or {}).get("name") or ""
    if name and email:
        return f"{name} <{email}>"
    return name or email or ""


def _addrs_to_list(items: list[dict[str, Any]] | None) -> list[str]:
    return [s for s in (_addr_to_str(a) for a in (items or [])) if s]


def _body_text(msg: dict[str, Any]) -> str:
    body = msg.get("body") or {}
    content = body.get("content") or ""
    if body.get("contentType", "").lower() == "html":
        return msg.get("bodyPreview") or content
    return content or msg.get("bodyPreview") or ""


def mail_messages_to_payload(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in raw:
        out.append({
            "id": m.get("id") or "",
            "internal_date": m.get("receivedDateTime") or "",
            "from": _addr_to_str(m.get("from")),
            "to": _addrs_to_list(m.get("toRecipients")),
            "cc": _addrs_to_list(m.get("ccRecipients")),
            "subject": m.get("subject") or "",
            "body_text": _body_text(m),
            "categories": m.get("categories") or [],
        })
    return out


def filter_mail_by_date(messages: list[dict[str, Any]], since_iso: str) -> list[dict[str, Any]]:
    return [m for m in messages if (m.get("receivedDateTime") or "") >= since_iso]


# ---------------------------------------------------------------------------
# Calendar (read)
# ---------------------------------------------------------------------------

def list_calendars(access_token: str) -> list[dict[str, Any]]:
    """List all calendars the user has access to (own + shared).

    Returns minimal calendar objects: id, name, ownership, can_share, can_edit,
    color. Shared calendars have owner.address != current user.
    """
    url = _odata_url(f"{GRAPH_BASE}/me/calendars", {
        "$top": "50",
        "$select": "id,name,owner,canShare,canEdit,canViewPrivateItems,hexColor",
    })
    return _graph_get_json(url, access_token).get("value", [])


def fetch_calendar_events(
    start_iso: str,
    end_iso: str,
    access_token: str,
    limit: int = MESSAGE_LIMIT,
    calendar_id: str | None = None,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Fetch events from a calendar between start and end. If calendar_id is
    None, queries the user's default calendar via /me/calendarView. Otherwise
    queries /me/calendars/{id}/calendarView (works for own + shared calendars).
    Follows @odata.nextLink up to max_pages.
    """
    select = ",".join([
        "id", "subject", "bodyPreview", "body", "organizer", "attendees",
        "start", "end", "location", "onlineMeeting", "categories",
        "webLink", "isCancelled", "showAs",
    ])
    base = (
        f"{GRAPH_BASE}/me/calendars/{calendar_id}/calendarView"
        if calendar_id
        else f"{GRAPH_BASE}/me/calendarView"
    )
    url = _odata_url(base, {
        "startDateTime": start_iso,
        "endDateTime": end_iso,
        "$top": str(limit),
        "$orderby": "start/dateTime",
        "$select": select,
    })
    return _paginate(url, access_token, prefer='outlook.body-content-type="text"',
                     max_pages=max_pages, context="calendar fetch")


def _fromisoformat_compat(text: Any) -> datetime | None:
    """datetime.fromisoformat shim for Python 3.10, where the parser rejects
    7-digit fractional seconds (Graph sends '.0000000') and the 'Z' suffix."""
    s = str(text).strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    head, dot, tail = s.partition(".")
    if dot:
        i = 0
        while i < len(tail) and tail[i].isdigit():
            i += 1
        if i > 6:
            tail = tail[:6] + tail[i:]
        s = head + dot + tail
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _normalize_graph_datetime(value: Any, timezone_name: Any) -> str:
    """Make Graph's offset-less UTC timestamps unambiguous for ingestion."""
    text = str(value or "")
    zone = str(timezone_name or "").strip().lower()
    if not text or zone not in {"", "utc"} or text.lower().endswith("z"):
        return text
    parsed = _fromisoformat_compat(text)
    if parsed is None or parsed.tzinfo is not None:
        return text
    return parsed.replace(microsecond=0).isoformat(timespec="seconds") + "Z"


def calendar_events_to_payload(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ev in raw:
        body = ev.get("body") or {}
        body_text = (
            ev.get("bodyPreview")
            if body.get("contentType", "").lower() == "html"
            else body.get("content") or ev.get("bodyPreview") or ""
        )
        start = ev.get("start") or {}
        end = ev.get("end") or {}
        out.append({
            "id": ev.get("id") or "",
            "subject": ev.get("subject") or "",
            "start": _normalize_graph_datetime(start.get("dateTime"), start.get("timeZone")),
            "end": _normalize_graph_datetime(end.get("dateTime"), end.get("timeZone")),
            "timezone": start.get("timeZone") or "",
            "organizer": _addr_to_str(ev.get("organizer")),
            "attendees": _addrs_to_list(ev.get("attendees")),
            "location": (ev.get("location") or {}).get("displayName") or "",
            "online_meeting_url": (ev.get("onlineMeeting") or {}).get("joinUrl") or "",
            "categories": ev.get("categories") or [],
            "is_cancelled": bool(ev.get("isCancelled")),
            "show_as": ev.get("showAs") or "",
            "web_link": ev.get("webLink") or "",
            "body_text": body_text,
        })
    return out


# ---------------------------------------------------------------------------
# Meetings + transcripts (corporate accounts with Teams)
# ---------------------------------------------------------------------------

def find_online_meeting_id(join_url: str, access_token: str) -> str | None:
    safe = join_url.replace("'", "''")
    url = _odata_url(f"{GRAPH_BASE}/me/onlineMeetings", {
        "$filter": f"JoinWebUrl eq '{safe}'",
    })
    try:
        data = _graph_get_json(url, access_token)
    except urllib.error.HTTPError:
        return None
    items = data.get("value") or []
    if not items:
        return None
    return items[0].get("id")


def list_transcripts(meeting_id: str, access_token: str) -> list[dict[str, Any]]:
    url = f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts"
    try:
        return _graph_get_json(url, access_token).get("value", [])
    except urllib.error.HTTPError:
        return []


def fetch_transcript_content(meeting_id: str, transcript_id: str, access_token: str) -> str:
    url = (
        f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts/{transcript_id}/content"
        f"?$format=text/vtt"
    )
    try:
        return _graph_get_text(url, access_token, accept="text/vtt")
    except urllib.error.HTTPError:
        return ""


def fetch_meetings_with_transcripts(
    start_iso: str,
    end_iso: str,
    access_token: str,
    limit: int = MESSAGE_LIMIT,
) -> list[dict[str, Any]]:
    """Pull calendar events that are Teams meetings, augment with transcript bodies."""
    events = fetch_calendar_events(start_iso, end_iso, access_token, limit=limit)
    out: list[dict[str, Any]] = []
    for ev in events:
        join_url = (ev.get("onlineMeeting") or {}).get("joinUrl") or ""
        if not join_url:
            continue
        meeting_id = find_online_meeting_id(join_url, access_token)
        if not meeting_id:
            out.append({"event": ev, "meeting_id": "", "transcripts": []})
            continue
        transcripts_raw = list_transcripts(meeting_id, access_token)
        transcripts = []
        for t in transcripts_raw:
            tid = t.get("id") or ""
            content = fetch_transcript_content(meeting_id, tid, access_token) if tid else ""
            transcripts.append({
                "id": tid,
                "created_at": t.get("createdDateTime") or "",
                "content_vtt": content,
            })
        out.append({
            "event": ev,
            "meeting_id": meeting_id,
            "transcripts": transcripts,
        })
    return out


def meetings_to_payload(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in raw:
        ev = entry.get("event") or {}
        body = ev.get("body") or {}
        body_text = (
            ev.get("bodyPreview")
            if body.get("contentType", "").lower() == "html"
            else body.get("content") or ev.get("bodyPreview") or ""
        )
        start = ev.get("start") or {}
        end = ev.get("end") or {}
        out.append({
            "id": ev.get("id") or "",
            "meeting_id": entry.get("meeting_id") or "",
            "subject": ev.get("subject") or "",
            "start": _normalize_graph_datetime(start.get("dateTime"), start.get("timeZone")),
            "end": _normalize_graph_datetime(end.get("dateTime"), end.get("timeZone")),
            "timezone": start.get("timeZone") or "",
            "organizer": _addr_to_str(ev.get("organizer")),
            "attendees": _addrs_to_list(ev.get("attendees")),
            "online_meeting_url": (ev.get("onlineMeeting") or {}).get("joinUrl") or "",
            "body_text": body_text,
            "transcripts": entry.get("transcripts") or [],
        })
    return out


# ---------------------------------------------------------------------------
# Actions (write): send mail, calendar event CRUD
# ---------------------------------------------------------------------------

def send_mail(
    to: list[str],
    subject: str,
    body: str,
    access_token: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    html: bool = False,
    save_to_sent: bool = True,
) -> None:
    """POST /me/sendMail. No response body on success (Graph returns 202)."""
    def _to_recipients(addrs: list[str] | None) -> list[dict[str, Any]]:
        return [{"emailAddress": {"address": a}} for a in (addrs or []) if a]

    payload = {
        "message": {
            "subject": subject,
            "body": {
                "contentType": "HTML" if html else "Text",
                "content": body,
            },
            "toRecipients": _to_recipients(to),
            "ccRecipients": _to_recipients(cc),
            "bccRecipients": _to_recipients(bcc),
        },
        "saveToSentItems": save_to_sent,
    }
    url = f"{GRAPH_BASE}/me/sendMail"
    _graph_write("POST", url, access_token, payload)


def create_event(
    subject: str,
    start_iso: str,
    end_iso: str,
    access_token: str,
    timezone: str = "UTC",
    attendees: list[str] | None = None,
    body: str = "",
    location: str = "",
    calendar_id: str | None = None,
) -> dict[str, Any]:
    """POST /me/events (or POST /me/calendars/{id}/events). Returns the created event object."""
    payload: dict[str, Any] = {
        "subject": subject,
        "start": {"dateTime": start_iso, "timeZone": timezone},
        "end": {"dateTime": end_iso, "timeZone": timezone},
    }
    if body:
        payload["body"] = {"contentType": "Text", "content": body}
    if location:
        payload["location"] = {"displayName": location}
    if attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"}
            for a in attendees if a
        ]
    base = (
        f"{GRAPH_BASE}/me/calendars/{calendar_id}/events"
        if calendar_id
        else f"{GRAPH_BASE}/me/events"
    )
    return _graph_write("POST", base, access_token, payload)


def update_event(
    event_id: str,
    access_token: str,
    subject: str | None = None,
    start_iso: str | None = None,
    end_iso: str | None = None,
    timezone: str = "UTC",
    add_attendees: list[str] | None = None,
    body: str | None = None,
    location: str | None = None,
    is_cancelled: bool | None = None,
) -> dict[str, Any]:
    """PATCH /me/events/{id} with only the fields the caller wants to change."""
    payload: dict[str, Any] = {}
    if subject is not None:
        payload["subject"] = subject
    if start_iso is not None:
        payload["start"] = {"dateTime": start_iso, "timeZone": timezone}
    if end_iso is not None:
        payload["end"] = {"dateTime": end_iso, "timeZone": timezone}
    if body is not None:
        payload["body"] = {"contentType": "Text", "content": body}
    if location is not None:
        payload["location"] = {"displayName": location}
    if add_attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"}
            for a in add_attendees if a
        ]
    if is_cancelled is not None:
        payload["isCancelled"] = is_cancelled
    if not payload:
        raise ValueError("update_event called with no fields to change")
    url = f"{GRAPH_BASE}/me/events/{event_id}"
    return _graph_write("PATCH", url, access_token, payload)


def delete_event(event_id: str, access_token: str) -> None:
    """DELETE /me/events/{id}. Returns nothing on success (204)."""
    url = f"{GRAPH_BASE}/me/events/{event_id}"
    _graph_write("DELETE", url, access_token, None)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_iso_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_guid(value: str, label: str) -> str:
    value = value.strip()
    if not GUID_RE.fullmatch(value):
        raise ValueError(f"{label} must be a GUID in 8-4-4-4-12 format")
    return value


def _validate_tenant_id(value: str) -> str:
    value = value.strip()
    if value in TENANT_ALIASES:
        return value
    return _validate_guid(value, "tenant-id")


def _write_config_file(config: dict[str, Any]) -> None:
    _ensure_config_dir()
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _chmod_private(CONFIG_PATH, 0o600)


def _show_effective_config(cfg: Config) -> None:
    shown = {
        "client_id": cfg.client_id,
        "tenant_id": cfg.tenant_id,
        "read_only": cfg.read_only,
        "teams": cfg.teams,
        "shared_calendars": cfg.shared_calendars,
        "output_dir": cfg.output_dir,
        "vault_root": cfg.vault_root,
        "redirect_port": cfg.redirect_port,
        "scopes": cfg.scopes,
        "graph_base": cfg.graph_base,
        "authority_base": cfg.authority_base,
    }
    print(f"Config file: {CONFIG_PATH}")
    for key, value in shown.items():
        source = cfg.sources.get(key, "env" if key in {"graph_base", "authority_base"} else "default")
        rendered = json.dumps(value, ensure_ascii=False)
        print(f"{key}: {rendered} (source: {source})")


def cmd_configure(args: argparse.Namespace) -> int:
    current = _read_config_file()
    updates: dict[str, Any] = {}
    if args.client_id is not None:
        updates["client_id"] = _validate_guid(args.client_id, "client-id")
    if args.tenant_id is not None:
        updates["tenant_id"] = _validate_tenant_id(args.tenant_id)
    for key in ("read_only", "teams", "shared_calendars"):
        value = getattr(args, key)
        if value is not None:
            updates[key] = value
    if args.output_dir is not None:
        updates["output_dir"] = validate_output_dir(args.output_dir)
    if args.vault_root is not None:
        updates["vault_root"] = str(Path(args.vault_root).expanduser().resolve())

    if updates:
        current.update(updates)
        _write_config_file(current)
        print(f"Configuration saved to {CONFIG_PATH}")
    elif not args.show:
        print("No changes requested. Use --show to inspect effective configuration.")

    if args.show:
        cfg = load_config(overrides=updates, require_client=False)
        _show_effective_config(cfg)
    return 0


def _vault_root_or_error(args: argparse.Namespace, cfg: Config) -> str:
    vault_root = getattr(args, "vault_root", None) or cfg.vault_root
    if not vault_root:
        print(
            "ERROR: vault root is required. Pass --vault-root or save it with "
            f"`{_command_hint('configure')} --vault-root <path>`. ",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return vault_root


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", type=int, default=7, help="Lookback window in days.")
    p.add_argument("--vault-root", default=None, help="Vault root path; optional when configured.")
    p.add_argument(
        "--client-id",
        default=None,
        help="Override MS_GRAPH_CLIENT_ID env var.",
    )


def _ahead_days(value: str) -> int:
    try:
        days = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer from 0 to 60") from exc
    if not 0 <= days <= 60:
        raise argparse.ArgumentTypeError("must be from 0 to 60")
    return days


def _add_auth_only_args(p: argparse.ArgumentParser) -> None:
    """For action subcommands that don't need vault-root or days."""
    p.add_argument(
        "--client-id",
        default=None,
        help="Override MS_GRAPH_CLIENT_ID env var.",
    )


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


def _require_client(cfg: Config) -> None:
    if not cfg.client_id:
        _print_missing_client_id()
        raise SystemExit(2)


def _block_read_only(action: str, cfg: Config, summary: dict[str, Any]) -> int | None:
    if not cfg.read_only:
        return None
    message = (
        f"BLOCKED: {action} is disabled by the read-only profile. "
        "No Microsoft data was changed. / "
        f"BLOQUEADO: {action} está deshabilitado por el perfil de solo lectura. "
        "No se modificaron datos de Microsoft."
    )
    print(message, file=sys.stderr)
    _audit_action(action, "blocked_read_only", summary)
    _log_event(action, "blocked_read_only")
    return EXIT_READ_ONLY_BLOCKED


def cmd_mail(args: argparse.Namespace) -> int:
    cfg = load_config(args.client_id, getattr(args, "vault_root", None))
    vault_root = _vault_root_or_error(args, cfg)
    access_token = get_access_token(cfg)

    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    since_iso = _iso_z(since)

    if args.scope_kind == "folder":
        folder_id = resolve_folder_id(args.scope, access_token)
        if not folder_id:
            available = ", ".join(list_folders(access_token)) or "(none returned)"
            print(
                f"ERROR: folder not found: {args.scope!r}\nAvailable folders: {available}",
                file=sys.stderr,
            )
            return 2
        raw = fetch_folder_messages(folder_id, since_iso, access_token)
    else:
        raw = search_messages(args.scope, access_token)
        raw = filter_mail_by_date(raw, since_iso)

    payload = {
        "kind": "mail",
        "folder_or_query": args.scope,
        "scope_kind": args.scope_kind,
        "days": args.days,
        "vault_root": vault_root,
        "output_dir": cfg.output_dir,
        "ingested_at_iso": _now_iso_local(),
        "messages": mail_messages_to_payload(raw),
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    cfg = load_config(args.client_id, getattr(args, "vault_root", None))
    vault_root = _vault_root_or_error(args, cfg)
    access_token = get_access_token(cfg)

    today = datetime.now().astimezone().date()
    start_date = today - timedelta(days=args.days - 1)
    end_date = today + timedelta(days=args.ahead)
    start_local = datetime.combine(start_date, datetime_time.min).astimezone()
    end_local = datetime.combine(end_date, datetime_time(23, 59, 59)).astimezone()
    start_iso = _iso_z(start_local.astimezone(timezone.utc))
    end_iso = _iso_z(end_local.astimezone(timezone.utc))

    raw = fetch_calendar_events(
        start_iso, end_iso, access_token,
        calendar_id=getattr(args, "calendar_id", None),
    )

    payload = {
        "kind": "calendar",
        "days": args.days,
        "ahead": args.ahead,
        "vault_root": vault_root,
        "output_dir": cfg.output_dir,
        "ingested_at_iso": _now_iso_local(),
        "date_range": [start_date.isoformat(), end_date.isoformat()],
        "calendar_id": getattr(args, "calendar_id", None) or "default",
        "events": calendar_events_to_payload(raw),
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    return 0


def cmd_list_calendars(args: argparse.Namespace) -> int:
    cfg = load_config(args.client_id)
    access_token = get_access_token(cfg)
    cals = list_calendars(access_token)
    # Human-readable table to stdout (no JSON, not piped to ingest.py)
    print(f"{'OWN/SHARED':<10}  {'NAME':<40}  ID")
    print("-" * 100)
    for cal in cals:
        owner = (cal.get("owner") or {}).get("address") or ""
        name = cal.get("name") or "(unnamed)"
        cid = cal.get("id") or ""
        # naive: if owner email matches /me, treat as own; otherwise shared
        is_shared = bool(owner) and not cal.get("canEdit", True)
        kind = "shared" if is_shared else "own"
        print(f"{kind:<10}  {name[:40]:<40}  {cid}")
    print(f"\nTotal: {len(cals)} calendar(s).")
    return 0


def cmd_send_mail(args: argparse.Namespace) -> int:
    cfg = load_config(args.client_id, require_client=False)
    blocked = _block_read_only("send-mail", cfg, {"subject": args.subject})
    if blocked is not None:
        return blocked
    body_text = args.body
    if args.body_file:
        body_text = Path(args.body_file).read_text(encoding="utf-8")

    to = _split_csv(args.to)
    cc = _split_csv(args.cc)
    bcc = _split_csv(args.bcc)
    if not to:
        print("ERROR: at least one --to recipient is required.", file=sys.stderr)
        return 2

    audit_summary = {
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": args.subject,
        "body_chars": len(body_text),
        "body_sha8": _body_sha8(body_text),
        "html": args.html,
        "save_to_sent": not args.no_save,
    }

    if args.dry_run:
        print("DRY RUN, mail NOT sent. Payload preview:")
        print(f"  To:      {', '.join(to)}")
        if cc:
            print(f"  Cc:      {', '.join(cc)}")
        if bcc:
            print(f"  Bcc:     {', '.join(bcc)}")
        print(f"  Subject: {args.subject}")
        print(f"  HTML:    {args.html}")
        print(f"  Body ({len(body_text)} chars):")
        preview = body_text if len(body_text) <= 400 else body_text[:400] + " ..."
        print("    " + preview.replace("\n", "\n    "))
        _audit_action("send-mail", "dry_run", audit_summary)
        return 0

    _require_client(cfg)
    access_token = get_access_token(cfg)

    try:
        send_mail(
            to=to, subject=args.subject, body=body_text,
            access_token=access_token,
            cc=cc, bcc=bcc, html=args.html,
            save_to_sent=not args.no_save,
        )
    except urllib.error.HTTPError as e:
        _audit_action("send-mail", "failure", audit_summary, error_code=f"http_{e.code}")
        raise

    _audit_action("send-mail", "success", audit_summary)
    print(f"Sent: {args.subject!r} to {len(to)} recipient(s)" + (f" (+{len(cc)} cc, +{len(bcc)} bcc)" if cc or bcc else ""))
    return 0


def cmd_event_create(args: argparse.Namespace) -> int:
    cfg = load_config(args.client_id, require_client=False)
    blocked = _block_read_only("event-create", cfg, {"subject": args.subject})
    if blocked is not None:
        return blocked
    body_text = args.body or ""
    if args.body_file:
        body_text = Path(args.body_file).read_text(encoding="utf-8")

    attendees = _split_csv(args.attendees)

    audit_summary = {
        "subject": args.subject,
        "start": args.start,
        "end": args.end,
        "timezone": args.timezone,
        "attendees": attendees,
        "location": args.location or "",
        "body_chars": len(body_text),
        "calendar_id": args.calendar_id,
    }

    if args.dry_run:
        print("DRY RUN, event NOT created. Payload preview:")
        print(f"  Subject:   {args.subject}")
        print(f"  Start:     {args.start} ({args.timezone})")
        print(f"  End:       {args.end} ({args.timezone})")
        if attendees:
            print(f"  Attendees: {', '.join(attendees)}")
        if args.location:
            print(f"  Location:  {args.location}")
        if body_text:
            preview = body_text if len(body_text) <= 200 else body_text[:200] + " ..."
            print(f"  Body:      {preview}")
        _audit_action("event-create", "dry_run", audit_summary)
        return 0

    _require_client(cfg)
    access_token = get_access_token(cfg)

    try:
        created = create_event(
            subject=args.subject,
            start_iso=args.start,
            end_iso=args.end,
            access_token=access_token,
            timezone=args.timezone,
            attendees=attendees,
            body=body_text,
            location=args.location or "",
            calendar_id=args.calendar_id,
        )
    except urllib.error.HTTPError as e:
        _audit_action("event-create", "failure", audit_summary, error_code=f"http_{e.code}")
        raise

    _audit_action("event-create", "success", audit_summary, result_id=created.get("id"))
    print(f"Created event {created.get('id')}: {created.get('subject')!r} at {(created.get('start') or {}).get('dateTime')}")
    return 0


def cmd_event_update(args: argparse.Namespace) -> int:
    cfg = load_config(args.client_id, require_client=False)
    blocked = _block_read_only("event-update", cfg, {"event_id": args.event_id})
    if blocked is not None:
        return blocked
    body_text = args.body
    if args.body_file:
        body_text = Path(args.body_file).read_text(encoding="utf-8")

    add_attendees = _split_csv(args.add_attendees) if args.add_attendees else None
    fields_changed = []
    if args.subject is not None:
        fields_changed.append("subject")
    if args.start is not None:
        fields_changed.append("start")
    if args.end is not None:
        fields_changed.append("end")
    if body_text is not None:
        fields_changed.append("body")
    if args.location is not None:
        fields_changed.append("location")
    if add_attendees:
        fields_changed.append("attendees")
    if args.cancel:
        fields_changed.append("isCancelled")

    if not fields_changed:
        print("ERROR: no fields to update. Provide at least one of --subject/--start/--end/--body/--body-file/--location/--add-attendees/--cancel.", file=sys.stderr)
        return 2

    # --cancel notifies attendees and removes the event from their calendars,
    # which is destructive enough to deserve the same gate as event-delete.
    if args.cancel and not args.yes:
        print(
            "ERROR: --cancel notifies attendees and removes the event from their "
            "calendars. Re-run with --yes to confirm.",
            file=sys.stderr,
        )
        return 2

    audit_summary = {
        "event_id": args.event_id,
        "fields_changed": fields_changed,
        "subject": args.subject,
        "start": args.start,
        "end": args.end,
        "timezone": args.timezone,
        "add_attendees": add_attendees,
        "location": args.location,
        "body_chars": len(body_text) if body_text is not None else None,
        "cancelled": bool(args.cancel),
    }

    if args.dry_run:
        print(f"DRY RUN, event {args.event_id} NOT updated. Fields to change: {', '.join(fields_changed)}")
        _audit_action("event-update", "dry_run", audit_summary)
        return 0

    _require_client(cfg)
    access_token = get_access_token(cfg)

    try:
        update_event(
            event_id=args.event_id,
            access_token=access_token,
            subject=args.subject,
            start_iso=args.start,
            end_iso=args.end,
            timezone=args.timezone,
            add_attendees=add_attendees,
            body=body_text,
            location=args.location,
            is_cancelled=True if args.cancel else None,
        )
    except urllib.error.HTTPError as e:
        _audit_action("event-update", "failure", audit_summary, error_code=f"http_{e.code}")
        raise

    _audit_action("event-update", "success", audit_summary, result_id=args.event_id)
    print(f"Updated event {args.event_id}: changed {', '.join(fields_changed)}")
    return 0


def cmd_event_delete(args: argparse.Namespace) -> int:
    audit_summary = {"event_id": args.event_id}
    cfg = load_config(args.client_id, require_client=False)
    blocked = _block_read_only("event-delete", cfg, audit_summary)
    if blocked is not None:
        return blocked

    if args.dry_run:
        print(f"DRY RUN, event {args.event_id} NOT deleted.")
        _audit_action("event-delete", "dry_run", audit_summary)
        return 0

    if not args.yes:
        print(
            f"ERROR: event-delete is destructive. Re-run with --yes to confirm "
            f"deletion of event {args.event_id}.",
            file=sys.stderr,
        )
        return 2

    _require_client(cfg)
    access_token = get_access_token(cfg)
    try:
        delete_event(args.event_id, access_token)
    except urllib.error.HTTPError as e:
        _audit_action("event-delete", "failure", audit_summary, error_code=f"http_{e.code}")
        raise
    _audit_action("event-delete", "success", audit_summary, result_id=args.event_id)
    print(f"Deleted event {args.event_id}")
    return 0


def cmd_meetings(args: argparse.Namespace) -> int:
    cfg = load_config(args.client_id, getattr(args, "vault_root", None))
    if cfg.read_only and not cfg.teams:
        # Block before any token request. Teams transcripts are not part of
        # the read-only profile unless the operator explicitly enabled teams.
        print(
            "BLOCKED: meetings is disabled because the read-only profile does not "
            "include Teams. No token was requested. Ask an administrator to enable "
            "Teams for this connector if your organization allows it. / "
            "BLOQUEADO: meetings está deshabilitado porque el perfil de solo lectura "
            "no incluye Teams. No se solicitó un token. Pide a un administrador que "
            "habilite Teams para este conector si tu organización lo permite.",
            file=sys.stderr,
        )
        summary = {"teams": cfg.teams, "read_only": cfg.read_only}
        _audit_action("meetings", "blocked_read_only", summary)
        _log_event("meetings", "blocked_read_only")
        return EXIT_READ_ONLY_BLOCKED
    vault_root = _vault_root_or_error(args, cfg)
    if cfg.is_personal_tenant:
        print(
            "WARNING: meetings subcommand requires Teams, which is not available on\n"
            "personal Microsoft accounts. Set MS_GRAPH_TENANT_ID to your corporate\n"
            "tenant ID. Proceeding anyway; expect zero results.",
            file=sys.stderr,
        )
    access_token = get_access_token(cfg)

    start = datetime.now(timezone.utc) - timedelta(days=args.days)
    end = datetime.now(timezone.utc)
    start_iso = _iso_z(start)
    end_iso = _iso_z(end)

    raw = fetch_meetings_with_transcripts(start_iso, end_iso, access_token)

    payload = {
        "kind": "meetings",
        "days": args.days,
        "vault_root": vault_root,
        "output_dir": cfg.output_dir,
        "ingested_at_iso": _now_iso_local(),
        "date_range": [start_iso[:10], end_iso[:10]],
        "meetings": meetings_to_payload(raw),
    }
    sys.stdout.write(json.dumps(payload))
    sys.stdout.write("\n")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"ingest-outlook fetch.py v{__version__}")
    return 0


class CheckResult:
    """Outcome of one diagnostic check. Carries both human-readable status
    and a remediation plan (auto fix callable + manual instruction list).
    """
    def __init__(
        self,
        status: str,
        name: str,
        detail: str,
        fix_auto: "Callable[[], tuple[bool, str]] | None" = None,
        fix_manual: list[str] | None = None,
    ):
        self.status = status  # "PASS" | "WARN" | "FAIL"
        self.name = name
        self.detail = detail
        self.fix_auto = fix_auto
        self.fix_manual = fix_manual


# ---------------------------------------------------------------------------
# Fix actions. Each returns (success: bool, message: str).
# ---------------------------------------------------------------------------

def _fix_create_config_dir() -> tuple[bool, str]:
    try:
        _ensure_config_dir()
        return True, f"Created {TOKEN_DIR}"
    except OSError as e:
        return False, f"Cannot create {TOKEN_DIR}: {e}"


def _fix_chmod_token() -> tuple[bool, str]:
    if os.name == "nt":
        return True, "Windows uses the user profile ACL; chmod is not applicable."
    try:
        os.chmod(TOKEN_PATH, 0o600)
        return True, f"Set {TOKEN_PATH} to mode 0o600"
    except OSError as e:
        return False, f"Cannot chmod {TOKEN_PATH}: {e}"


def _fix_full_reauth(client_id_arg: str | None = None) -> tuple[bool, str]:
    """Delete the cached token and run a fresh OAuth flow. Opens browser.
    Used when token is missing, expired without refresh, rejected, or
    has insufficient scopes.
    """
    try:
        cfg = load_config(client_id_arg)
    except SystemExit:
        return False, "MS_GRAPH_CLIENT_ID not set; fix that first."
    _delete_token()
    print(f"  Opening browser to re-authenticate (Ctrl+C to cancel)...", file=sys.stderr)
    try:
        token = _run_oauth_flow(cfg)
        _save_token(token)
        return True, f"Re-authenticated. Token cached, expires in {int(token.get('expires_in', 0))}s."
    except SystemExit:
        return False, "OAuth flow was cancelled or timed out."
    except urllib.error.HTTPError as e:
        return False, f"OAuth flow failed at token endpoint: HTTP {e.code}"
    except Exception as e:
        return False, f"OAuth flow failed: {type(e).__name__}: {e}"


def _fix_create_vault(vault_path: str) -> tuple[bool, str]:
    try:
        Path(vault_path).mkdir(parents=True, exist_ok=True)
        return True, f"Created vault directory at {vault_path}"
    except OSError as e:
        return False, f"Cannot create {vault_path}: {e}"


# ---------------------------------------------------------------------------
# Diagnostic check runner (reusable by doctor + fix)
# ---------------------------------------------------------------------------

def run_checks(vault_root: str | None = None, client_id_arg: str | None = None) -> list[CheckResult]:
    """Run all diagnostic checks. Returns a list of CheckResult so callers
    (cmd_doctor for read-only display, cmd_fix for auto-remediation) can
    inspect and act on the outcomes.

    Checks are intentionally ordered so earlier failures don't poison later
    ones (e.g., no env -> skip auth check rather than crash).
    """
    PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
    results: list[CheckResult] = []

    cfg = load_config(client_id_arg, vault_root, require_client=False)
    client_id = cfg.client_id
    vault_root = vault_root or cfg.vault_root

    # 1. CONFIG
    if _config_file_error:
        results.append(CheckResult(
            FAIL, "config",
            f"config.json inválido en {CONFIG_PATH}: {_config_file_error}",
            fix_manual=[
                "The configuration file exists but is not a JSON object.",
                f"Regenerate it with: {_command_hint('configure')} --client-id <guid> --tenant-id <guid>",
                "corre `configure` para regenerarlo",
            ],
        ))
    elif not client_id:
        results.append(CheckResult(
            FAIL, "config",
            "Microsoft Graph client ID is not configured.",
            fix_manual=[
                "Get the Application (client) ID from your Azure app registration.",
                f"Run: {_command_hint('configure')} --client-id <guid> --tenant-id <guid>",
                "On macOS/Linux you may instead export MS_GRAPH_CLIENT_ID in ~/.zshrc.",
                f"Then re-run: {_command_hint('fix')}",
            ],
        ))
    else:
        source = cfg.sources.get("client_id", "unknown")
        results.append(CheckResult(
            PASS, "config", f"client ID loaded from {source}, tenant={cfg.tenant_id}"
        ))

    # 2. CONFIG DIR
    if not TOKEN_DIR.is_dir():
        results.append(CheckResult(
            FAIL, "config-dir",
            f"{TOKEN_DIR} does not exist.",
            fix_auto=_fix_create_config_dir,
        ))
    else:
        results.append(CheckResult(PASS, "config-dir", f"{TOKEN_DIR} exists"))

    # 3. TOKEN
    token = _load_token()
    if not token:
        results.append(CheckResult(
            WARN, "token",
            f"No cached token at {TOKEN_PATH}. Will need to authenticate.",
            fix_auto=(lambda: _fix_full_reauth(client_id_arg)) if client_id else None,
            fix_manual=None if client_id else ["Fix env first, then re-run fix to authenticate."],
        ))
    else:
        expires_at = token.get("expires_at", 0)
        seconds_left = int(expires_at - time.time())
        if seconds_left > 60:
            results.append(CheckResult(PASS, "token", f"cached, expires in {seconds_left}s"))
        elif token.get("refresh_token"):
            results.append(CheckResult(PASS, "token", "expired but refresh_token available (auto-refresh on next call)"))
        else:
            results.append(CheckResult(
                FAIL, "token",
                "Expired and no refresh_token available.",
                fix_auto=(lambda: _fix_full_reauth(client_id_arg)) if client_id else None,
            ))

    # 4. TOKEN FILE PERMISSIONS
    if TOKEN_PATH.is_file():
        if os.name == "nt":
            results.append(CheckResult(
                PASS, "token-perms", "Windows: protegido por el ACL del perfil de usuario"
            ))
        else:
            mode = oct(TOKEN_PATH.stat().st_mode & 0o777)
            if mode == "0o600":
                results.append(CheckResult(PASS, "token-perms", f"mode {mode} (owner-read-write only)"))
            else:
                results.append(CheckResult(
                    FAIL, "token-perms",
                    f"Token file is mode {mode}; expected 0o600 (anyone on this machine could read your refresh token).",
                    fix_auto=_fix_chmod_token,
                ))

    # 5. NETWORK
    try:
        net_req = urllib.request.Request(f"{cfg.graph_base}/$metadata")
        t0 = time.time()
        with urllib.request.urlopen(net_req, timeout=10) as resp:
            _ = resp.read(1)
        latency = int((time.time() - t0) * 1000)
        results.append(CheckResult(PASS, "network", f"Graph endpoint reachable ({latency}ms)"))
    except Exception as e:
        results.append(CheckResult(
            FAIL, "network",
            f"Cannot reach Graph endpoint: {e}",
            fix_manual=[
                "Check your internet connection.",
                "If you're on a corporate network, verify a proxy or firewall is not blocking *.microsoft.com.",
                "Try: ping graph.microsoft.com",
                f"After connection is restored, re-run: {_command_hint('fix')}",
            ],
        ))

    # 6. AUTH (only if env + valid token + network up)
    access_token: str | None = None
    network_ok = any(r.name == "network" and r.status == PASS for r in results)
    if client_id and token and token.get("refresh_token") and network_ok:
        try:
            access_token = get_access_token(cfg)
            me = _graph_get_json(f"{cfg.graph_base}/me", access_token)
            who = me.get("userPrincipalName") or me.get("mail") or me.get("displayName") or "(unknown)"
            results.append(CheckResult(PASS, "auth", f"token validates as {who}"))
        except urllib.error.HTTPError as e:
            results.append(CheckResult(
                FAIL, "auth",
                f"Graph rejected token (HTTP {e.code}). Re-authentication required.",
                fix_auto=(lambda: _fix_full_reauth(client_id_arg)),
            ))
        except SystemExit:
            pass  # env check above already covers this
        except Exception as e:
            results.append(CheckResult(
                FAIL, "auth",
                f"Unexpected error during auth check: {type(e).__name__}: {e}",
                fix_auto=(lambda: _fix_full_reauth(client_id_arg)),
            ))

    # 7. SCOPES
    if token and access_token is not None:
        expected = cfg.scopes
        ok, missing = verify_scopes(token, expected)
        if not token.get("scopes_granted"):
            results.append(CheckResult(
                WARN, "scopes",
                "Legacy token without scopes_granted field. Re-auth to enable scope verification.",
                fix_auto=(lambda: _fix_full_reauth(client_id_arg)),
            ))
        elif ok:
            results.append(CheckResult(PASS, "scopes", f"all {len(expected)} expected scopes granted"))
        else:
            results.append(CheckResult(
                FAIL, "scopes",
                f"Missing scopes: {', '.join(missing)}.",
                fix_auto=(lambda: _fix_full_reauth(client_id_arg)),
                fix_manual=[
                    f"If re-auth doesn't grant the missing scopes, add them in Azure:",
                    "  portal.azure.com → Microsoft Entra ID → App registrations → your app",
                    f"  → API permissions → Add: {', '.join(missing)}",
                    "Then re-run fix.",
                ],
            ))

    # 8. VAULT
    if vault_root:
        vault = Path(vault_root)
        if not vault.exists():
            results.append(CheckResult(
                FAIL, "vault",
                f"{vault} does not exist.",
                fix_auto=(lambda: _fix_create_vault(vault_root)),
            ))
        elif not os.access(vault, os.W_OK):
            results.append(CheckResult(
                FAIL, "vault",
                f"{vault} is not writable.",
                fix_manual=[
                    f"Check ownership: ls -la {vault.parent}",
                    f"If wrong owner: sudo chown -R $USER {vault}",
                    "Re-run fix after.",
                ],
            ))
        else:
            results.append(CheckResult(PASS, "vault", f"{vault} writable"))
    else:
        results.append(CheckResult(PASS, "vault", "(no --vault-root provided; skipping check)"))

    # 9. LOG / AUDIT files writable
    try:
        _ensure_config_dir()
        log_ok = audit_ok = True
        try:
            with LOG_PATH.open("a", encoding="utf-8"):
                pass
            results.append(CheckResult(PASS, "log.jsonl", f"writable at {LOG_PATH}"))
        except OSError as e:
            log_ok = False
            results.append(CheckResult(FAIL, "log.jsonl", f"not writable: {e}", fix_auto=_fix_create_config_dir))
        try:
            with AUDIT_PATH.open("a", encoding="utf-8"):
                pass
            results.append(CheckResult(PASS, "audit.jsonl", f"writable at {AUDIT_PATH}"))
        except OSError as e:
            audit_ok = False
            results.append(CheckResult(FAIL, "audit.jsonl", f"not writable: {e}", fix_auto=_fix_create_config_dir))
    except OSError as e:
        results.append(CheckResult(FAIL, "log/audit", f"cannot write to {TOKEN_DIR}: {e}", fix_auto=_fix_create_config_dir))

    return results


def _print_check_results(results: list[CheckResult]) -> tuple[int, int, int]:
    """Print check results in the same format as before. Returns (passed, warned, failed)."""
    passed = warned = failed = 0
    for r in results:
        tag = f"[{r.status}]"
        print(f"  {tag} {r.name:<14} {r.detail}")
        if r.status == "PASS":
            passed += 1
        elif r.status == "WARN":
            warned += 1
        else:
            failed += 1
    print(f"\nSummary: {passed} passed, {warned} warning, {failed} failed.")
    return passed, warned, failed


def cmd_doctor(args: argparse.Namespace) -> int:
    """Self-diagnostic. Read-only: reports issues but does not attempt fixes.
    Use `fetch.py fix` to auto-repair where possible.
    """
    print(f"ingest-outlook doctor (v{__version__})\n")
    results = run_checks(getattr(args, "vault_root", None))
    passed, warned, failed = _print_check_results(results)
    if failed > 0:
        print(f"\nTo auto-repair, run: {_command_hint('fix')}")
    _log_event("doctor", "success" if failed == 0 else "failure", extra={"passed": passed, "warned": warned, "failed": failed})
    return 0 if failed == 0 else 1


def cmd_fix(args: argparse.Namespace) -> int:
    """Self-healing entry point. Runs diagnostic checks, applies automatic
    fixes where possible, prints explicit manual steps for the rest,
    re-verifies, and reports.

    End-user UX: this is THE command to run when anything is wrong.

    In --quiet mode (used by session-start hooks): suppresses normal output,
    emits a single summary line to stderr only if action was needed, exits
    silently otherwise. Lets the harness run fix on every session start
    without spamming the user.
    """
    quiet = getattr(args, "quiet", False) or _quiet_mode

    def out(msg: str = "") -> None:
        if not quiet:
            print(msg)

    out(f"ingest-outlook fix (v{__version__})\n")
    out("Step 1/3: Running diagnostics...\n")
    results = run_checks(getattr(args, "vault_root", None))
    if not quiet:
        _print_check_results(results)

    issues = [r for r in results if r.status in ("WARN", "FAIL")]
    if not issues:
        out("\nNothing to fix. System is healthy.")
        _log_event("fix", "success", extra={"issues_found": 0, "quiet": quiet})
        return 0

    out(f"\nStep 2/3: Addressing {len(issues)} issue(s)...\n")
    auto_fixed = manual_required = auto_failed = 0
    auto_fixed_names: list[str] = []
    manual_names: list[str] = []
    for i, issue in enumerate(issues, 1):
        out(f"[{i}/{len(issues)}] {issue.name}")
        out(f"    Problem: {issue.detail}")
        if issue.fix_auto is not None:
            try:
                success, msg = issue.fix_auto()
            except Exception as e:
                success, msg = False, f"Fix raised {type(e).__name__}: {e}"
            mark = "+" if success else "x"
            out(f"    Auto-fix: [{mark}] {msg}")
            if success:
                auto_fixed += 1
                auto_fixed_names.append(issue.name)
            else:
                auto_failed += 1
                manual_names.append(issue.name)
                if issue.fix_manual:
                    out("    Manual steps:")
                    for step in issue.fix_manual:
                        out(f"      {step}")
        elif issue.fix_manual:
            out("    Cannot auto-fix. Manual steps:")
            for step in issue.fix_manual:
                out(f"      {step}")
            manual_required += 1
            manual_names.append(issue.name)
        else:
            out("    (No remediation defined; this is informational only.)")
        out("")

    out("Step 3/3: Re-verifying...\n")
    results2 = run_checks(getattr(args, "vault_root", None))
    if not quiet:
        _print_check_results(results2)
    passed2 = sum(1 for r in results2 if r.status == "PASS")
    failed2 = sum(1 for r in results2 if r.status == "FAIL")

    out("")
    if failed2 == 0:
        out(f"System healthy. Auto-fixed {auto_fixed} issue(s).")
        if manual_required > 0:
            out(f"Note: {manual_required} item(s) had only manual instructions; verify them.")
        if quiet and auto_fixed > 0:
            # In quiet mode, still report what was healed so logs/observers can see action was taken
            print(
                f"ingest-outlook fix: auto-repaired {auto_fixed} ({', '.join(auto_fixed_names)})",
                file=sys.stderr,
            )
        _log_event("fix", "success", extra={"auto_fixed": auto_fixed, "auto_failed": auto_failed, "manual_required": manual_required, "quiet": quiet})
        return 0
    else:
        out(f"{failed2} issue(s) still need attention. Follow the manual steps printed above, then re-run fix.")
        if quiet:
            # Quiet mode: surface a single line so the hook can show it
            print(
                f"ingest-outlook fix: {failed2} issue(s) need attention ({', '.join(manual_names)}). Run: {_command_hint('fix')}",
                file=sys.stderr,
            )
        _log_event("fix", "partial", extra={"auto_fixed": auto_fixed, "auto_failed": auto_failed, "manual_required": manual_required, "remaining": failed2, "quiet": quiet})
        return 1


def main() -> int:
    global _run_started_at
    _configure_console_encoding()
    _run_started_at = time.time()

    parser = argparse.ArgumentParser(
        description="Fetch Outlook data via Microsoft Graph for ingest-outlook.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"ingest-outlook fetch.py v{__version__}",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print debug info to stderr (URLs, retries, token expiry).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress normal output. Used by session-start hooks.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_config = subparsers.add_parser(
        "configure",
        help="Write config.json or show the effective configuration and value sources.",
    )
    p_config.add_argument("--client-id", default=None, help="Application (client) GUID.")
    p_config.add_argument("--tenant-id", default=None, help="Tenant GUID or common/organizations/consumers.")
    ro_group = p_config.add_mutually_exclusive_group()
    ro_group.add_argument("--read-only", dest="read_only", action="store_true", default=None)
    ro_group.add_argument("--read-write", dest="read_only", action="store_false")
    teams_group = p_config.add_mutually_exclusive_group()
    teams_group.add_argument("--teams", dest="teams", action="store_true", default=None)
    teams_group.add_argument("--no-teams", dest="teams", action="store_false")
    shared_group = p_config.add_mutually_exclusive_group()
    shared_group.add_argument("--shared-calendars", dest="shared_calendars", action="store_true", default=None)
    shared_group.add_argument("--no-shared-calendars", dest="shared_calendars", action="store_false")
    p_config.add_argument("--output-dir", default=None, help="Relative output directory inside the vault.")
    p_config.add_argument("--vault-root", default=None, help="Default vault root path.")
    p_config.add_argument("--show", action="store_true", help="Print effective values and their sources.")
    p_config.set_defaults(func=cmd_configure)

    p_mail = subparsers.add_parser("mail", help="Fetch Outlook messages.")
    p_mail.add_argument("--scope", required=True, help="Folder name or search query.")
    p_mail.add_argument(
        "--scope-kind",
        choices=["folder", "query"],
        required=True,
        help="Whether scope is a folder name or $search query.",
    )
    _add_common_args(p_mail)
    p_mail.set_defaults(func=cmd_mail)

    p_cal = subparsers.add_parser("calendar", help="Fetch calendar events.")
    p_cal.add_argument(
        "--calendar-id",
        default=None,
        help="Optional: read events from a specific calendar (use list-calendars to get IDs). Defaults to your default calendar.",
    )
    p_cal.add_argument(
        "--ahead",
        type=_ahead_days,
        default=0,
        help="Include this many future local calendar days (0-60; default: 0).",
    )
    _add_common_args(p_cal)
    p_cal.set_defaults(func=cmd_calendar)

    p_meet = subparsers.add_parser(
        "meetings",
        help="Fetch Teams meetings + transcripts (corporate accounts only).",
    )
    _add_common_args(p_meet)
    p_meet.set_defaults(func=cmd_meetings)

    # --- Read: list-calendars ---
    p_listcal = subparsers.add_parser(
        "list-calendars",
        help="List all calendars (own + shared with me). Human-readable output, not piped to ingest.py.",
    )
    _add_auth_only_args(p_listcal)
    p_listcal.set_defaults(func=cmd_list_calendars)

    # --- Action: send-mail ---
    p_send = subparsers.add_parser("send-mail", help="Send an email via /me/sendMail.")
    p_send.add_argument("--to", required=True, help="Comma-separated recipient emails.")
    p_send.add_argument("--subject", required=True, help="Subject line.")
    p_send.add_argument("--body", default="", help="Body text (or use --body-file).")
    p_send.add_argument("--body-file", default=None, help="Read body from file (overrides --body).")
    p_send.add_argument("--cc", default="", help="Comma-separated Cc emails.")
    p_send.add_argument("--bcc", default="", help="Comma-separated Bcc emails.")
    p_send.add_argument("--html", action="store_true", help="Send body as HTML instead of text.")
    p_send.add_argument("--no-save", action="store_true", help="Do NOT save to Sent Items (default: save).")
    p_send.add_argument("--dry-run", action="store_true", help="Print the payload without sending.")
    _add_auth_only_args(p_send)
    p_send.set_defaults(func=cmd_send_mail)

    # --- Action: event-create ---
    p_ec = subparsers.add_parser("event-create", help="Create a calendar event.")
    p_ec.add_argument("--subject", required=True)
    p_ec.add_argument("--start", required=True, help="ISO 8601 start, e.g. 2026-05-22T14:00:00")
    p_ec.add_argument("--end", required=True, help="ISO 8601 end, e.g. 2026-05-22T15:00:00")
    p_ec.add_argument("--timezone", default="UTC", help='IANA timezone, e.g. "America/Bogota". Default UTC.')
    p_ec.add_argument("--attendees", default="", help="Comma-separated attendee emails.")
    p_ec.add_argument("--body", default="", help="Event body / description.")
    p_ec.add_argument("--body-file", default=None, help="Read body from file (overrides --body).")
    p_ec.add_argument("--location", default="", help="Location display name.")
    p_ec.add_argument("--calendar-id", default=None, help="Optional target calendar. Defaults to your default calendar.")
    p_ec.add_argument("--dry-run", action="store_true", help="Print the payload without creating.")
    _add_auth_only_args(p_ec)
    p_ec.set_defaults(func=cmd_event_create)

    # --- Action: event-update ---
    p_eu = subparsers.add_parser("event-update", help="Update an existing calendar event (PATCH).")
    p_eu.add_argument("--event-id", required=True, help="Outlook event ID (from /ingest-outlook calendar).")
    p_eu.add_argument("--subject", default=None)
    p_eu.add_argument("--start", default=None, help="New ISO 8601 start.")
    p_eu.add_argument("--end", default=None, help="New ISO 8601 end.")
    p_eu.add_argument("--timezone", default="UTC")
    p_eu.add_argument("--add-attendees", default=None, help="Comma-separated emails to set as attendees (REPLACES the existing list).")
    p_eu.add_argument("--body", default=None, help="New event body.")
    p_eu.add_argument("--body-file", default=None, help="Read new body from file.")
    p_eu.add_argument("--location", default=None, help="New location display name.")
    p_eu.add_argument("--cancel", action="store_true", help="Mark the event as cancelled (notifies attendees; requires --yes).")
    p_eu.add_argument("--yes", action="store_true", help="Required when using --cancel (notifies attendees).")
    p_eu.add_argument("--dry-run", action="store_true", help="Print the field list without patching.")
    _add_auth_only_args(p_eu)
    p_eu.set_defaults(func=cmd_event_update)

    # --- Action: event-delete ---
    p_ed = subparsers.add_parser("event-delete", help="Delete a calendar event (destructive).")
    p_ed.add_argument("--event-id", required=True)
    p_ed.add_argument("--yes", action="store_true", help="Required to actually delete; otherwise the command exits 2.")
    p_ed.add_argument("--dry-run", action="store_true", help="Print what would be deleted without deleting.")
    _add_auth_only_args(p_ed)
    p_ed.set_defaults(func=cmd_event_delete)

    # --- Diagnostic: doctor (read-only) ---
    p_doc = subparsers.add_parser(
        "doctor",
        help="Read-only diagnostic checks (config, token, network, scopes, vault, log files). Does NOT attempt fixes.",
    )
    p_doc.add_argument(
        "--vault-root",
        default=None,
        help="Optional vault path to check writability.",
    )
    p_doc.set_defaults(func=cmd_doctor)

    # --- Self-healing: fix ---
    p_fix = subparsers.add_parser(
        "fix",
        help="Self-heal: diagnose, auto-repair what can be auto-repaired, print explicit manual steps for the rest, re-verify. Run this when anything is wrong.",
    )
    p_fix.add_argument(
        "--vault-root",
        default=None,
        help="Optional vault path to check (and create if missing).",
    )
    p_fix.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress normal output; emit one-line summary to stderr only when action was needed. Use for session-start hooks.",
    )
    p_fix.set_defaults(func=cmd_fix)

    # --- Diagnostic: version ---
    p_ver = subparsers.add_parser("version", help="Print the script version.")
    p_ver.set_defaults(func=cmd_version)

    args = parser.parse_args()
    command_name = args.command

    global _verbose_mode, _quiet_mode
    _verbose_mode = bool(getattr(args, "verbose", False))
    # quiet is also set per-subcommand (fix has its own --quiet); honor both
    _quiet_mode = bool(getattr(args, "quiet", False))

    sos = f"\nFor step-by-step recovery, run:\n  {_command_hint('fix')}"

    try:
        result = args.func(args)
    except urllib.error.HTTPError as e:
        duration_ms = int((time.time() - _run_started_at) * 1000) if _run_started_at else None
        _log_event(command_name, "failure", duration_ms=duration_ms, error_code=f"http_{e.code}")
        # Don't print SOS for fix/doctor itself (they ARE the recovery)
        if command_name not in ("fix", "doctor"):
            print(sos, file=sys.stderr)
        return 2
    except SystemExit as e:
        duration_ms = int((time.time() - _run_started_at) * 1000) if _run_started_at else None
        _log_event(command_name, "failure", duration_ms=duration_ms, error_code="config_missing")
        # SOS already printed by load_config in this path
        raise
    except ValueError as e:
        duration_ms = int((time.time() - _run_started_at) * 1000) if _run_started_at else None
        _log_event(command_name, "failure", duration_ms=duration_ms, error_code="invalid_config")
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        duration_ms = int((time.time() - _run_started_at) * 1000) if _run_started_at else None
        _log_event(command_name, "failure", duration_ms=duration_ms, error_code=type(e).__name__)
        if command_name not in ("fix", "doctor"):
            print(sos, file=sys.stderr)
        raise

    duration_ms = int((time.time() - _run_started_at) * 1000) if _run_started_at else None
    status = "success" if result == 0 else "failure"
    _log_event(command_name, status, duration_ms=duration_ms, extra={"exit_code": result})
    return result


if __name__ == "__main__":
    sys.exit(main())
