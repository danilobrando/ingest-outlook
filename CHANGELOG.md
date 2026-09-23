# Changelog

## 0.5.1

- `doctor`/`fix`: optional scopes that Microsoft may legitimately not grant (`Calendars.Read.Shared`, `OnlineMeetings.Read`, `OnlineMeetingTranscript.Read.All`) are now a WARN without auto-fix instead of a FAIL that re-opened the browser on every `fix`. Found on the first real sign-in with a personal Microsoft account. Missing core scopes still FAIL.
- First end-to-end run against a real account: sign-in, `calendar --days 1 --ahead 1` (UTC → local time correct) and `mail`.

## 0.5.0

- Fixed UTC calendar timestamps without a `Z` suffix being rendered as local time.
- Added `calendar --ahead 0..60`; calendar windows now use local-day boundaries, so the lookback starts at 00:00 local and the range can include future days.
- Added a persistent `configure` command with explicit value-source reporting.
- Added the corporate read-only profile and local blocking for all Graph write commands.
- Added configurable vault/output paths and backward-compatible ingestion payloads.
- Added Windows-safe console, paths, filenames, token checks, locking behavior, and no-admin PowerShell installation.
- Fixed false scope drift by checking the effective configured scopes.
- Fixed Windows permission diagnostics that previously attempted ineffective `chmod` repairs.
- Replaced hardcoded recovery paths with the active interpreter and resolved script path.
- Added fake Graph/token integration tests and Linux, macOS, and Windows CI coverage.
- Added corporate Entra setup and Spanish Windows installation documentation.
- Stopped treating omitted Entra protocol scopes (`offline_access`, `openid`, `profile`, `email`) as missing. Scope comparison is case-insensitive and strips the `https://graph.microsoft.com/` prefix from granted scopes, so `fix` no longer re-authenticates in a loop.
- Read-only mode discards `ReadWrite` and `.Send` scopes from `MS_GRAPH_SCOPES` (`.Read.All` is kept), warns on stderr, and `configure --show` reports the filtered source.
- `meetings` in the read-only profile without Teams is blocked before a token request (exit 3, `blocked_read_only`).
- `config.json` is read as UTF-8 with BOM. Invalid JSON or a non-object is reported on stderr and fails the doctor `config` check instead of being ignored.
- AADSTS50011 now names the configured redirect URI, including a custom loopback port.
- Windows `install.ps1` skips WindowsApps Python stubs (`Path`, then `Source`), uses literal paths (PowerShell 5.1 has no `New-Item -LiteralPath`), honors `INGEST_OUTLOOK_CONFIG_DIR`, copies directory contents without nesting `docs\docs`, excludes `.git`, `tests`, `.github`, and `__pycache__`, and checks `$LASTEXITCODE` after Python.
- Windows CI runs the installer twice, fails on nested `docs\docs` or a missing `fetch.py`, and repeats the install on a path with a space, an accent, and brackets.

## 0.4.0

- Initial public release.
