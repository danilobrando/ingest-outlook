"""Shared helpers for the ingest-outlook tests (no network, temp dirs only)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FETCH = ROOT / "fetch.py"
INGEST = ROOT / "ingest.py"
LOCK_CLI = ROOT / "cerebro_lock.py"
CLIENT_ID = "11111111-1111-4111-8111-111111111111"
TENANT_ID = "22222222-2222-4222-8222-222222222222"
READ_ONLY_SCOPES = ["User.Read", "Mail.Read", "Calendars.Read", "offline_access"]

ENV_KEYS = (
    "MS_GRAPH_CLIENT_ID", "MS_GRAPH_TENANT_ID", "MS_GRAPH_SCOPES",
    "INGEST_OUTLOOK_REDIRECT_PORT", "INGEST_OUTLOOK_READ_ONLY",
    "INGEST_OUTLOOK_OUTPUT_DIR", "INGEST_OUTLOOK_VAULT_ROOT",
    "INGEST_OUTLOOK_GRAPH_BASE", "INGEST_OUTLOOK_AUTHORITY_BASE",
    "INGEST_OUTLOOK_PROFILE", "INGEST_OUTLOOK_SYNC_BUDGET_SECONDS",
    "CEREBRO_LOCK_WAIT_SECONDS", "CEREBRO_LOCK_STALE_SECONDS", "CEREBRO_LOCK_POLL_SECONDS",
    # The connector must configure its own console encoding; the tests
    # must not force UTF-8 via the parent environment.
    "PYTHONIOENCODING", "PYTHONUTF8",
)


def clean_env(config_dir: Path, server=None) -> dict[str, str]:
    env = os.environ.copy()
    for key in ENV_KEYS:
        env.pop(key, None)
    env["INGEST_OUTLOOK_CONFIG_DIR"] = str(config_dir)
    if server:
        env["INGEST_OUTLOOK_GRAPH_BASE"] = server.base_url
        env["INGEST_OUTLOOK_AUTHORITY_BASE"] = server.base_url
    return env


def write_config(config_dir: Path, vault: Path, **extra) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "client_id": CLIENT_ID,
        "tenant_id": TENANT_ID,
        "read_only": True,
        "vault_root": str(vault),
    }
    data.update(extra)
    (config_dir / "config.json").write_text(json.dumps(data), encoding="utf-8")


def write_token(config_dir: Path, access_token="seed-access", expired=False) -> None:
    token = {
        "access_token": access_token,
        "refresh_token": "seed-refresh",
        "expires_at": time.time() - 60 if expired else time.time() + 3600,
        "scopes_granted": READ_ONLY_SCOPES,
    }
    (config_dir / "token.json").write_text(json.dumps(token), encoding="utf-8")
    if os.name != "nt":
        os.chmod(config_dir / "token.json", 0o600)


def run_fetch(env: dict[str, str], *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FETCH), *args],
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=timeout,
    )


def load_fetch_module(config_dir: Path):
    old = os.environ.get("INGEST_OUTLOOK_CONFIG_DIR")
    os.environ["INGEST_OUTLOOK_CONFIG_DIR"] = str(config_dir)
    try:
        name = f"fetch_test_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, FETCH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            os.environ.pop("INGEST_OUTLOOK_CONFIG_DIR", None)
        else:
            os.environ["INGEST_OUTLOOK_CONFIG_DIR"] = old
