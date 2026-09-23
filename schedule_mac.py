"""
schedule_mac.py, macOS LaunchAgent support for `fetch.py schedule`.

Author:   Danny Bravo
License:  MIT
Project:  https://github.com/danilobrando/ingest-outlook

One per-user LaunchAgent per vault:
`~/Library/LaunchAgents/com.cerebro.ingesta.<hash-of-vault>.plist`, running
`<python> <fetch.py> sync --all --vault-root <vault>` every StartInterval
seconds (1800 by default). Loaded with `launchctl bootstrap gui/<uid>`,
removed with `launchctl bootout`, inspected with `launchctl print`.

The plist is built here (testable without loading anything). Stdlib only.
"""
from __future__ import annotations

__author__ = "Danny Bravo"
__version__ = "0.6.0"
__license__ = "MIT"

import hashlib
import os
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any

LABEL_PREFIX = "com.cerebro.ingesta"


def vault_hash(vault: str | os.PathLike[str]) -> str:
    normalized = os.path.normcase(os.path.abspath(os.path.expanduser(str(vault))))
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]


def label_for(vault: str | os.PathLike[str]) -> str:
    return f"{LABEL_PREFIX}.{vault_hash(vault)}"


def launch_agents_dir() -> Path:
    override = os.environ.get("INGEST_OUTLOOK_LAUNCHAGENTS_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / "Library" / "LaunchAgents"


def plist_path(vault: str | os.PathLike[str]) -> Path:
    return launch_agents_dir() / f"{label_for(vault)}.plist"


def program_arguments(python: str, fetch_path: str, vault: str, profile: str | None = None) -> list[str]:
    if profile:
        return [python, fetch_path, "--profile", profile, "sync", "--vault-root", vault]
    return [python, fetch_path, "sync", "--all", "--vault-root", vault]


def build_plist(
    python: str,
    fetch_path: str,
    vault: str,
    every_minutes: int = 30,
    log_path: str | None = None,
    environment: dict[str, str] | None = None,
    profile: str | None = None,
) -> bytes:
    if not 5 <= int(every_minutes) <= 720:
        raise ValueError("--every must be between 5 and 720 minutes")
    data: dict[str, Any] = {
        "Label": label_for(vault),
        "ProgramArguments": program_arguments(python, fetch_path, vault, profile),
        "StartInterval": int(every_minutes) * 60,
        "RunAtLoad": False,
        "WorkingDirectory": str(Path(fetch_path).parent),
        "ProcessType": "Background",
    }
    if log_path:
        data["StandardOutPath"] = log_path
        data["StandardErrorPath"] = log_path
    if environment:
        data["EnvironmentVariables"] = dict(environment)
    return plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=True)


def gui_domain() -> str:
    return f"gui/{os.getuid()}"


def run_launchctl(args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def parse_print(output: str) -> dict[str, str]:
    """Pick the few fields of `launchctl print` a person cares about."""
    wanted = {"state": "estado", "runs": "corridas", "last exit code": "ultimo_resultado"}
    found: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^\s*([a-z ]+?)\s*=\s*(.+?)\s*$", line)
        if match and match.group(1) in wanted and wanted[match.group(1)] not in found:
            found[wanted[match.group(1)]] = match.group(2)
    return found
