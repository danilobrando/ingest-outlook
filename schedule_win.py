"""
schedule_win.py, Windows Task Scheduler support for `fetch.py schedule`.

Author:   Danny Bravo
License:  MIT
Project:  https://github.com/danilobrando/ingest-outlook

One task per vault, `\\Rewired\\Cerebro - ingesta`, for the current user,
without a password and only while the user is signed in (LogonType
Interactive, RunLevel Limited: no elevation). Weekly trigger Monday-Friday at
`start`, repeated every `every` minutes for `hours` hours. Action:
`pythonw.exe "<fetch.py>" sync --all` (pythonw so no console window opens).

The PowerShell is built here as plain text (testable on any OS) and run with
-EncodedCommand, so paths with spaces, accents or apostrophes need no shell
quoting. Stdlib only.
"""
from __future__ import annotations

__author__ = "Danny Bravo"
__version__ = "0.6.0"
__license__ = "MIT"

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TASK_NAME = "Cerebro - ingesta"
TASK_FOLDER = "\\Rewired\\"
ROOT_FOLDER = "\\"
EXIT_DENIED = 5
EXIT_NOT_INSTALLED = 3
EXECUTION_LIMIT_MINUTES = 20

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

# LastTaskResult values worth translating for a person.
RESULT_TEXT = {
    0: "ok",
    1: "error (revisa el log de ingesta del vault)",
    2: "error de configuración",
    4: "requiere inicio de sesión: ejecuta fix --profile <perfil>",
    267008: "lista para correr",
    267009: "corriendo ahora",
    267011: "todavía no ha corrido",
    267014: "detenida por el usuario",
    2147750687: "ya había una corrida en curso (se ignoró esta)",
    3221225786: "interrumpida",
}


@dataclass
class ScheduleSpec:
    execute: str
    arguments: str
    working_dir: str
    start: str = "07:00"
    every_minutes: int = 30
    hours: int = 12
    task_name: str = TASK_NAME
    task_folder: str = TASK_FOLDER


def validate_start(value: str) -> str:
    match = _TIME_RE.fullmatch(str(value).strip())
    if not match:
        raise ValueError("--start must be HH:MM (24 h), e.g. 07:00")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def ps_quote(value: str) -> str:
    """PowerShell single-quoted literal: only the apostrophe needs doubling."""
    return "'" + str(value).replace("'", "''") + "'"


def win_quote_arg(value: str) -> str:
    """Quote one argument for the Windows argv parser (CommandLineToArgvW).

    Backslashes before the closing quote are doubled so `C:\\` stays `C:\\`.
    """
    text = str(value)
    if text and not re.search(r'[\s"]', text):
        return text
    text = re.sub(r'(\\*)"', lambda m: m.group(1) * 2 + '\\"', text)
    text = re.sub(r"(\\+)$", lambda m: m.group(1) * 2, text)
    return f'"{text}"'


def pythonw_for(python_exe: str) -> tuple[str, bool]:
    """pythonw.exe next to the resolved python.exe; falls back to python itself."""
    exe = Path(python_exe)
    if exe.name.lower() == "pythonw.exe":
        return str(exe), True
    candidate = exe.with_name("pythonw.exe")
    if candidate.is_file():
        return str(candidate), True
    return str(exe), False


def build_action_arguments(fetch_path: str, profile: str | None = None, vault_root: str | None = None) -> str:
    parts = [win_quote_arg(fetch_path)]
    if profile:
        parts += ["--profile", profile, "sync"]
    else:
        parts += ["sync", "--all"]
    if vault_root:
        parts += ["--vault-root", win_quote_arg(vault_root)]
    return " ".join(parts)


def _header() -> list[str]:
    return [
        "$ErrorActionPreference = 'Stop'",
        "try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }",
    ]


def build_register_script(spec: ScheduleSpec) -> str:
    start = validate_start(spec.start)
    if not 5 <= int(spec.every_minutes) <= 720:
        raise ValueError("--every must be between 5 and 720 minutes")
    if not 1 <= int(spec.hours) <= 24:
        raise ValueError("--hours must be between 1 and 24")
    # +1 minute so the last run (e.g. 19:00 for 07:00 + 12 h) is included.
    duration_minutes = int(spec.hours) * 60 + 1
    lines = _header() + [
        f"$execute = {ps_quote(spec.execute)}",
        f"$arguments = {ps_quote(spec.arguments)}",
        f"$workdir = {ps_quote(spec.working_dir)}",
        f"$taskName = {ps_quote(spec.task_name)}",
        f"$folders = @({ps_quote(spec.task_folder)}, {ps_quote(ROOT_FOLDER)})",
        # The SID works for standard, local, domain and Entra-joined users alike.
        "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        "$action = New-ScheduledTaskAction -Execute $execute -Argument $arguments -WorkingDirectory $workdir",
        "$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday "
        f"-At {ps_quote(start)}",
        f"$repeat = New-ScheduledTaskTrigger -Once -At {ps_quote(start)} "
        f"-RepetitionInterval (New-TimeSpan -Minutes {int(spec.every_minutes)}) "
        f"-RepetitionDuration (New-TimeSpan -Minutes {duration_minutes})",
        "$trigger.Repetition = $repeat.Repetition",
        "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited",
        "$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew "
        f"-ExecutionTimeLimit (New-TimeSpan -Minutes {EXECUTION_LIMIT_MINUTES}) "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        "$denied = $false",
        "foreach ($folder in $folders) {",
        "  try {",
        "    $task = Register-ScheduledTask -TaskName $taskName -TaskPath $folder -Action $action "
        "-Trigger $trigger -Principal $principal -Settings $settings "
        "-Description 'Cerebro: ingesta de correo, calendario y reuniones (ingest-outlook sync)' -Force",
        "    Write-Output ('REGISTERED|' + $task.TaskPath + $task.TaskName)",
        "    exit 0",
        "  } catch {",
        "    $message = [string]$_.Exception.Message",
        "    $hresult = $_.Exception.HResult",
        "    if ($hresult -eq -2147024891 -or $message -match 'denied|denegado|0x80070005') {",
        "      $denied = $true",
        "      Write-Output ('DENIED|' + $folder + '|' + $message)",
        "    } else {",
        "      Write-Output ('ERROR|' + $folder + '|' + $message)",
        "    }",
        "    # ANY failure in the \\Rewired\\ folder falls back to the root folder.",
        "    continue",
        "  }",
        "}",
        f"if ($denied) {{ exit {EXIT_DENIED} }}",
        "exit 1",
    ]
    return "\n".join(lines) + "\n"


def _find_task_lines(task_name: str, task_folder: str) -> list[str]:
    return [
        f"$taskName = {ps_quote(task_name)}",
        f"$folders = @({ps_quote(task_folder)}, {ps_quote(ROOT_FOLDER)})",
        "$found = $null",
        "foreach ($folder in $folders) {",
        "  $candidate = Get-ScheduledTask -TaskName $taskName -TaskPath $folder -ErrorAction SilentlyContinue",
        "  if ($candidate) { $found = $candidate; break }",
        "}",
    ]


def build_status_script(task_name: str = TASK_NAME, task_folder: str = TASK_FOLDER) -> str:
    lines = _header() + _find_task_lines(task_name, task_folder) + [
        "if (-not $found) { Write-Output '{\"instalada\": false}'; exit 3 }",
        "$info = $found | Get-ScheduledTaskInfo",
        "$last = ''",
        "if ($info.LastRunTime -and $info.LastRunTime.Year -gt 2000) { $last = $info.LastRunTime.ToString('yyyy-MM-dd HH:mm') }",
        "$next = ''",
        "if ($info.NextRunTime) { $next = $info.NextRunTime.ToString('yyyy-MM-dd HH:mm') }",
        "$actions = @($found.Actions | ForEach-Object { [string]$_.Execute + ' ' + [string]$_.Arguments }) -join ' ; '",
        "$result = [ordered]@{",
        "  instalada = $true",
        "  ruta = [string]$found.TaskPath + [string]$found.TaskName",
        "  estado = [string]$found.State",
        "  ultima = $last",
        "  proxima = $next",
        "  resultado = [int64]$info.LastTaskResult",
        "  accion = $actions",
        "}",
        "Write-Output ($result | ConvertTo-Json -Compress)",
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def build_remove_script(task_name: str = TASK_NAME, task_folder: str = TASK_FOLDER) -> str:
    lines = _header() + [
        f"$taskName = {ps_quote(task_name)}",
        f"$folders = @({ps_quote(task_folder)}, {ps_quote(ROOT_FOLDER)})",
        "$removed = 0",
        "foreach ($folder in $folders) {",
        "  $candidate = Get-ScheduledTask -TaskName $taskName -TaskPath $folder -ErrorAction SilentlyContinue",
        "  if ($candidate) {",
        "    Unregister-ScheduledTask -TaskName $taskName -TaskPath $folder -Confirm:$false",
        "    $removed++",
        "  }",
        "}",
        "Write-Output ('REMOVED|' + $removed)",
        "exit 0",
    ]
    return "\n".join(lines) + "\n"


def powershell_exe() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(candidate) if candidate.is_file() else "powershell"


def powershell_command(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        powershell_exe(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", encoded,
    ]


def run_powershell(script: str, timeout: int = 120) -> tuple[int, str, str]:
    proc = subprocess.run(powershell_command(script), capture_output=True, timeout=timeout)
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    return proc.returncode, stdout, stderr


def parse_status(stdout: str) -> dict:
    for line in reversed([l.strip() for l in stdout.splitlines() if l.strip()]):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {}


def describe_result(code: int | None) -> str:
    if code is None:
        return "desconocido"
    value = int(code) & 0xFFFFFFFF
    return RESULT_TEXT.get(value, f"código {value} (0x{value:08X})")
