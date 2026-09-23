# Changelog

## 0.6.0

- **Profiles**: global `--profile <slug>` (before or after the subcommand) and `INGEST_OUTLOOK_PROFILE`. Each profile keeps config, token, logs, audit, refresh lock, watermark state and id indices in `<config-dir>/<slug>/`. No profile keeps the v0.5.1 behavior.
- **Cerebro layout**: new configuration keys `layout` (`legacy` default, `cerebro`), `empresa` (mandatory for cerebro, a slug), `raw_root` (`raw/entradas`), `lock_path` (`.claude/system3/cerebro.lock`), `ingest_log_path` (`.claude/system3/logs/ingesta.log`), `backfill_days` (30), `calendar_past_days` (1), `calendar_ahead_days` (14), `mail_exclude_folders` (`junkemail,deleteditems,drafts`), `max_messages_per_run` (1000). `configure --registros-dir <dir>` sets the lock and log paths in one go. Every vault path is relative and validated (no absolute paths, no `..`, no Windows reserved names; `.claude` allowed).
- **`sync`**: non-interactive ingestion for scheduled runs. Whole mailbox by `receivedDateTime` watermark (excluded folders and their subfolders skipped), one immutable file per message with the contract frontmatter and the full text body (100 KB cap); calendar day files from yesterday to +14 days, rewritten only when they change; Teams transcripts as `.md` (one line per intervention) plus the original `.vtt`, with the `SpeakerAttributionNotAllowed` fallback and `403 GraphAccessToTranscriptsDisabled` logged without stopping mail or calendar. `--all` runs every cerebro profile of the vault alphabetically under one lock; `--dry-run` writes nothing; `--only` limits the sources. 8-minute budget per run; the next run continues from the watermark. Participants are normalized (`Name <email>`, lowercase, deduplicated, sender or organizer first, rooms excluded). The connector never writes `sintetico`.
- **Never a browser from automation**: when the refresh token stops working, `sync` writes a `needs-login` marker, logs `requiere-login`, and exits 4. `fix --profile <slug>` signs in again and clears it; `doctor` reports it.
- **`cerebro_lock.py`**: one O_EXCL lock per vault shared with the extraction and s3 (JSON `paso, perfil, pid, inicio, equipo`; abandoned after 20 min and stolen; busy for 5 min means skip). Importable module plus `acquire|release|status` CLI. `sync` refuses to run when the profiles of a vault or `.claude/system3/config.json` disagree on `lock_path`.
- **`schedule install|remove|status`**: Windows `\Rewired\Cerebro - ingesta` via `Register-ScheduledTask` (current user, interactive only, no elevation, Mon-Fri 07:00-19:00 every 30 min, `pythonw.exe`, StartWhenAvailable, IgnoreNew, 20-min limit, runs on battery); access denied exits 5 and points to the startup-hook fallback. macOS: per-user LaunchAgent `com.cerebro.ingesta.<hash>` every 30 min. Other systems: message, exit 0.
- **`install.ps1`**: `-Profile`, `-Empresa`, `-Layout cerebro`, `-Teams`, `-Schedule` (plus optional `-RawRoot`, `-LockPath`, `-IngestLogPath`); existing parameters unchanged.
- **Doctor**: `needs-login`, `empresa` and `vault-lock` checks.
- **Tests and CI**: the fake Graph moved to `tests/fake_graph.py` (module and script); v0.6 coverage for the raw-file contract, immutability, watermarks, caps, calendar rewrites, Teams, exit 4 without a browser, one lock for `--all`, budget, concurrency, stolen and busy locks, dry-run, profiles and scheduling. Windows CI installs two profiles with `install.ps1 -Layout cerebro`, runs both syncs in parallel against the fake Graph, and registers, inspects and removes the scheduled task. Python 3.14 added to the matrix.
- Docs: generic installation guide, IT app-registration guide, startup-hook fallback and Teams transcripts research.

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
