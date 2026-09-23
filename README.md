# ingest-outlook

> **Author:** Danny Bravo · **License:** MIT · **Repo:** [danilobrando/ingest-outlook](https://github.com/danilobrando/ingest-outlook)

A Claude Code skill that connects Microsoft 365 (Outlook mail, calendar, Teams meetings) to a local Obsidian-style vault, with **autonomous self-healing**: when something breaks, the user just says "no funciona" and the system diagnoses and repairs itself.

- **Reads**: Outlook mail, calendar events (own + shared), Teams meetings with transcripts
- **Optional writes**: send mail, create/update/delete calendar events when the read-write profile is enabled
- **Self-heals**: token refresh, scope drift, file permissions, missing dirs all get fixed automatically
- **Privacy**: everything runs locally, no third-party services; tokens use mode 0600 on POSIX and the user-profile ACL on Windows
- **Audit**: every write action logged to `~/.config/ingest-outlook/audit.jsonl`
- **Stdlib only**: Python 3.10+, no `pip install` required
- **Corporate-safe profile**: read-only mode requests only mail/calendar read permissions by default and blocks every write command locally
- **Cerebro layout (v0.6)**: one vault per person reading 1..N Microsoft 365 tenants (one profile each), synced automatically every 30 minutes by a Windows task or a macOS LaunchAgent, under one lock per vault

## Quick start

This connector follows the **second-brain connector standard**: all connectors live under `~/second-brain/connectors/` and Claude Code discovers them through a symlink at `~/.claude/skills/`. Three commands:

```bash
mkdir -p ~/second-brain/connectors
cd ~/second-brain/connectors
git clone https://github.com/danilobrando/ingest-outlook.git
cd ingest-outlook
./install.sh
```

The installer:
1. Warns when it is outside the conventional location (`~/second-brain/connectors/ingest-outlook/`), but continues
2. Verifies Python 3.10+ is present
3. Creates the symlink `~/.claude/skills/ingest-outlook` → the cloned repo (Claude Code discovers it through this path)
4. Creates `~/.config/ingest-outlook/` at mode 0700 for the token cache
5. Smoke-tests `fetch.py`
6. Prints the remaining configuration steps

> The repo lives in your vault (under `~/second-brain/connectors/`) so it travels with your vault backups. Tokens and logs live in `~/.config/ingest-outlook/` and stay machine-local.

After install:

1. **Register an Azure app** (one-time, 5 minutes). See [`docs/azure-app-setup.md`](docs/azure-app-setup.md).
2. **Export two env vars** in your shell:
   ```bash
   echo 'export MS_GRAPH_CLIENT_ID="<your-application-client-id>"' >> ~/.zshrc
   echo 'export MS_GRAPH_TENANT_ID="common"' >> ~/.zshrc   # or your corporate tenant UUID
   source ~/.zshrc
   ```
3. **Verify health**:
   ```bash
   python ~/.claude/skills/ingest-outlook/fetch.py fix
   ```
   The first run opens a browser for OAuth consent. After that, the token is cached and refreshed automatically.

That's it. The skill is now loaded into your Claude Code agent, and you can interact in natural language.

## Windows (corporate, read-only)

For a managed corporate account, the recommended setup is the read-only profile. Open PowerShell and run these commands, replacing `<cargo>`, `<client-id>` and `<tenant-id>` with the values for your vault and tenant:

```powershell
git clone --branch v0.6.0 https://github.com/danilobrando/ingest-outlook.git "$env:LOCALAPPDATA\ingest-outlook"
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ingest-outlook\install.ps1" -VaultRoot "$env:USERPROFILE\cerebros\<cargo>" -ClientId "<client-id>" -TenantId "<tenant-id>" -ReadOnly
py -3 "$env:USERPROFILE\cerebros\<cargo>\.claude\skills\ingest-outlook\fetch.py" fix
```

The third command completes the first browser login.

If `py -3` is unavailable, use `python`, provided it resolves to Python 3.10+ and not a `WindowsApps` Microsoft Store stub. The Application (client) ID and Directory (tenant) ID are public identifiers, not passwords or secrets; the company's IT administrator creates the app in the company's own tenant and gives users those two IDs. See the [Spanish Windows installation guide](docs/instalacion-windows-es.md).

## v0.6: cerebro layout (profiles, `sync`, `schedule`)

The legacy layout above (`External Inputs/Outlook/...`, fed by `fetch.py | ingest.py`) is still the default. v0.6 adds a second layout, `cerebro`, for vaults that follow the System 3 canonical layout: raw inputs land under `raw/entradas/`, and a separate extraction process turns them into project notes.

**Profiles.** One profile per Microsoft 365 tenant, as many as the person needs. `--profile <slug>` (before or after the subcommand, or `INGEST_OUTLOOK_PROFILE`) keeps config, token, logs, watermark and indices in `~/.config/ingest-outlook/<slug>/`. Nothing of this goes into the vault.

```bash
fetch.py configure --profile acme --empresa acme --layout cerebro \
  --client-id <guid> --tenant-id <guid> --read-only [--teams] --vault-root ~/cerebros/<cargo>
fetch.py fix --profile acme            # first sign-in (opens the browser)
fetch.py sync --all --dry-run          # what would be written, writes nothing
fetch.py sync --all                    # every cerebro profile of the vault, one lock
fetch.py schedule install              # Windows task or macOS LaunchAgent
```

**What `sync` writes** (paths relative to the vault; every path is configurable per profile):

| Source | Path | Rule |
|---|---|---|
| Mail (whole mailbox minus junk, deleted items, drafts) | `raw/entradas/correo/<empresa>/YYYY-MM-DD/HHMM-<subject-slug>.md` | One file per message, immutable, full text body up to 100 KB, attachment names only |
| Calendar (yesterday to +14 days) | `raw/entradas/calendario/<empresa>/YYYY-MM-DD.md` | One file per day, rewritten when it changes; cancelled and deleted events are reflected |
| Teams transcripts (`teams: true`) | `raw/entradas/reuniones/<empresa>/YYYY-MM-DD-<slug>.md` + `.vtt` | Immutable; `[HH:MM:SS] Name: text` per intervention |
| Run log | `.claude/system3/logs/ingesta.log` | One line per profile and run, no personal data |
| Vault lock | `.claude/system3/cerebro.lock` | Shared with the extraction and the s3 index (`cerebro_lock.py`) |

Frontmatter keys are Spanish and fixed by the shared contract: `fuente, empresa, cuenta, fecha, id_origen, participantes, asunto, proyecto` (+ `carpeta, hilo, adjuntos, ingestado` for mail; `organizador, vtt` for meetings). `empresa` is mandatory and always equals the `<empresa>` folder. `participantes` are normalized to `Name <email>` (lowercase email, deduplicated, sender or organizer first, meeting rooms moved to `lugar`).

**Configuration keys** (`configure --show --profile <slug>` prints each value with its source): `layout` (`legacy` | `cerebro`), `empresa`, `raw_root` (`raw/entradas`), `lock_path` (`.claude/system3/cerebro.lock`), `ingest_log_path` (`.claude/system3/logs/ingesta.log`), `--registros-dir <dir>` (shortcut for both), `backfill_days` (30), `calendar_past_days` (1), `calendar_ahead_days` (14), `mail_exclude_folders` (`junkemail,deleteditems,drafts`), `max_messages_per_run` (1000).

**Behavior worth knowing:**

- The first run backfills `backfill_days` of mail; after that it is incremental by `receivedDateTime` watermark. A run stops cleanly after 8 minutes or `max_messages_per_run` messages and the next run continues.
- One lock per vault: created with O_EXCL; abandoned after 20 minutes and stolen; if busy, `sync` waits up to 5 minutes and then skips the run (exit 0, `estado saltada`). `sync` refuses to run if the profiles of a vault, or the vault's `.claude/system3/config.json`, disagree on `lock_path`.
- `sync` never opens a browser. When a refresh token stops working it writes `needs-login` in the profile, logs `requiere-login`, and exits **4**; a person runs `fix --profile <slug>` to sign in again.
- Exit codes: 0 ok or skipped, 1 error, 2 configuration, 4 needs sign-in (`sync --all` returns the worst).
- Teams: a `403 GraphAccessToTranscriptsDisabled` is logged with that code and does not stop mail or calendar. See [`docs/teams-transcripts-research.md`](docs/teams-transcripts-research.md).
- `schedule` on Windows registers `\Rewired\Cerebro - ingesta` for the current user (only while signed in, no elevation, Mon-Fri 07:00-19:00 every 30 minutes, `pythonw.exe`); if company policy denies it, see [`docs/respaldo-hook-inicio.md`](docs/respaldo-hook-inicio.md). On macOS it installs `~/Library/LaunchAgents/com.cerebro.ingesta.<hash>.plist` (every 30 minutes). Elsewhere it prints a message.

Guides (Spanish): [installation](docs/instalacion-cerebro-es.md), [app registration for the IT admin](docs/ti-registro-app-es.md), [startup-hook fallback](docs/respaldo-hook-inicio.md).

## What you can ask your Claude Code agent

Once installed, you don't run commands directly. You talk to your agent:

| You say | Agent does |
|---|---|
| "Trae mis correos de hoy" / "fetch today's mail" | Runs `fetch.py mail` and writes to your vault |
| "Qué reuniones tengo mañana?" | Runs `fetch.py calendar --days 1 --ahead 1` and shows you |
| "Mándale un correo a X" | Runs `fetch.py send-mail` only in the optional read-write profile |
| "Agéndame una llamada con Y el jueves a las 3" | Runs `fetch.py event-create` only in the optional read-write profile |
| "No me llegan correos" / "no funciona" | Silently runs `fetch.py fix`, then either confirms repair or walks you through manual steps |

The trigger phrases for self-healing are documented in [`SKILL.md`](SKILL.md). Both Spanish and English work.

## Architecture

```
your message ──► Claude Code agent
                     │
                     │ reads SKILL.md (decides what to do)
                     │
                     ├── for normal asks ──► fetch.py <subcommand> | ingest.py
                     │                            │
                     │                            ├── OAuth2 PKCE (cached, auto-refreshed)
                     │                            ├── Microsoft Graph REST API
                     │                            └── writes vault markdown OR Graph mutation
                     │
                     └── for failures ──► fetch.py fix --quiet
                                              │
                                              ├── diagnoses 9 checks
                                              ├── auto-repairs what it can
                                              └── prints next steps for the rest
```

Scripts (all stdlib only):
- **`fetch.py`** — the single CLI entry point: OAuth2 + Microsoft Graph REST, profiles, `sync`, `schedule`, `doctor`/`fix`.
- **`ingest.py`** — normalizes Graph JSON responses into legacy-layout vault markdown.
- **`sync_cerebro.py`** — writes the cerebro raw layer (used by `fetch.py sync`).
- **`cerebro_lock.py`** — the vault lock, importable and with its own CLI for the other cerebro processes.
- **`schedule_win.py`**, **`schedule_mac.py`** — Windows Task Scheduler and macOS LaunchAgent support.

Single contract file:
- **`SKILL.md`** — read by Claude Code at session start. Defines trigger phrases, auto-recovery policy, and how the agent should react to user-reported problems.

## Subcommands

```
fetch.py mail --scope <folder|query> --scope-kind <folder|query> --days N [--vault-root <path>]
fetch.py calendar [--calendar-id <id>] --days N [--ahead 0..60] [--vault-root <path>]
fetch.py meetings --days N [--vault-root <path>]          # corporate accounts only
fetch.py list-calendars                                    # own + shared
fetch.py send-mail --to "nobody@example.invalid" --subject "..." --body "..." [--cc, --bcc, --html, --dry-run]
fetch.py event-create --subject "..." --start ISO --end ISO [--timezone, --attendees, --body, --location]
fetch.py event-update --event-id <id> [--subject, --start, --end, --add-attendees, --body, --location, --cancel --yes]
fetch.py event-delete --event-id <id> --yes
fetch.py doctor [--vault-root <path>]                      # read-only diagnostic
fetch.py fix [--vault-root <path>] [--quiet]               # diagnose + auto-repair
fetch.py configure [options] [--show]                       # persistent cross-platform config
fetch.py sync [--all | --profile <slug>] [--vault-root <path>] [--dry-run] [--only mail|calendar|meetings]
fetch.py schedule install|remove|status [--vault-root <path>] [--every 30] [--start 07:00] [--hours 12] [--dry-run]
fetch.py version
```

All commands accept `--verbose` for debug output to stderr and `--profile <slug>` to select a profile.

`calendar` uses local-day boundaries: `--days 1 --ahead 1` requests today from 00:00 through tomorrow at 23:59:59, then sends the corresponding UTC range to Microsoft Graph. `--ahead` defaults to `0`.

## Self-healing

The connector's defining feature. When something goes wrong, the user runs ONE command (or, with the SessionStart hook, runs nothing at all):

```bash
python ~/.claude/skills/ingest-outlook/fetch.py fix
```

`fix` runs 9 diagnostic checks (effective config, config dir, token freshness, file permissions, network, auth, scope drift, vault writable, log/audit files writable). For each failing check, it either auto-repairs or prints the exact manual command needed. Then it re-verifies.

| Failure mode | Auto-fix? |
|---|---|
| Config directory missing | Yes (`mkdir -p`) |
| Token file mode wrong (not 0o600) | Yes on POSIX (`chmod 600`); Windows uses the user-profile ACL |
| Token expired but refresh available | Yes (already automatic at every call) |
| Token rejected by Graph | Yes (delete + re-OAuth, opens browser) |
| Scope drift | Yes (re-OAuth with full scope set) |
| Vault directory missing | Yes (`mkdir -p`) |
| Log/audit files not writable | Yes |
| Client ID missing from config and environment | No; prints the exact `configure` command |
| Network unreachable | No; prints connection check steps |
| Clock skew | No (needs admin/sudo); prints System Settings path |

## Proactive auto-recovery (recommended)

Add this to your `~/.claude/settings.json` so `fix --quiet` runs at every Claude Code session start. Token-about-to-expire? Refreshed before you notice. Scope drift? Caught proactively.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python ~/.claude/skills/ingest-outlook/fetch.py fix --quiet --vault-root ~/your-vault-path || true"
          }
        ]
      }
    ]
  }
}
```

`--quiet` mode is silent when everything is healthy. If `fix` auto-repaired something, it emits one line to stderr. If something needs your attention, it emits one line indicating what.

## Privacy & security

- **Local-only**: all data and tokens stay on your machine. No third-party servers.
- **Token storage**: `~/.config/ingest-outlook/token.json`, protected by mode `0o600` on POSIX and the user-profile ACL on Windows.
- **Audit log**: every write action (send-mail, event-create/update/delete) recorded with timestamp, recipients, subject, body hash (not body content) to `~/.config/ingest-outlook/audit.jsonl`.
- **Read PII safeguards**: in the legacy layout, ingested mail bodies are truncated to 500 chars in the vault. Calendar bodies to 400 chars. Transcripts are stored verbatim (the whole point). The cerebro layout stores the full plain-text mail body (100 KB cap), because the downstream extraction needs it.
- **Sensitive paths warning**: do not let `~/.config/` be synced by cloud providers (iCloud, OneDrive, MDM profiles). The connector's `fix` command detects most cases.

## Required Microsoft Graph permissions

The recommended corporate read-only profile requests only `User.Read`, `Mail.Read`, `Calendars.Read`, and `offline_access`. It does not request `Mail.Send`, `Calendars.ReadWrite`, shared-calendar access, or Teams transcript access. The broader permissions below apply only to the optional read-write/Teams configuration.

Delegated permissions (act as the signed-in user only, never as application):

| Permission | Used for |
|---|---|
| `User.Read` | Identify the signed-in user |
| `offline_access` | Refresh tokens (no re-auth every hour) |
| `Mail.Read` | Read your own mail |
| `Mail.Send` | Send mail in your name |
| `Calendars.ReadWrite` | Read + create/modify your own calendar events |
| `Calendars.Read.Shared` | Read calendars others have shared with you |
| `OnlineMeetings.Read` | Read Teams meeting metadata (corporate only) |
| `OnlineMeetingTranscript.Read.All` | Read Teams meeting transcripts (corporate, requires admin consent) |

Personal Microsoft accounts auto-use a narrower set (no Teams scopes). The skill adapts based on `MS_GRAPH_TENANT_ID`.

See [`docs/azure-app-setup.md`](docs/azure-app-setup.md) for the full registration walkthrough.

## Troubleshooting

When in doubt: `python ~/.claude/skills/ingest-outlook/fetch.py fix`. That's the answer to almost everything.

If `fix` itself doesn't help:

1. Verify Python version: `python3 --version` (needs 3.10+).
2. Verify env vars: `echo $MS_GRAPH_CLIENT_ID` should print a GUID.
3. Verify Azure app: log into the Azure portal, find the app, confirm the redirect URI is exactly `http://localhost:8765/callback` and "Allow public client flows" is set to Yes.
4. Force a fresh OAuth: `rm ~/.config/ingest-outlook/token.json && python ~/.claude/skills/ingest-outlook/fetch.py fix`
5. Inspect logs: `tail ~/.config/ingest-outlook/log.jsonl`

## Family of connectors

This skill is part of the `ingest-*` family pattern from [ai-brain-starter](https://github.com/adelaidasofia/ai-brain-starter). Same output contract (`External Inputs/<Source>/...`), same idempotency-per-day model. Other connectors in the family include `ingest-gmail`, `ingest-slack`, `ingest-linear`, `ingest-notion`.

The architectural difference: most of those rely on an upstream MCP server. Microsoft Graph has no MCP, so this skill includes its own OAuth + REST client.

## Contributing

Issues and pull requests welcome.

## Author

**Danny Bravo** — product strategist, AI ecosystem builder, educator. Builder of [Tribu iA](https://tribuia.org) and the Hardcore AI program.

## License

MIT. See [`LICENSE`](LICENSE). Copyright © 2026 Danny Bravo.
