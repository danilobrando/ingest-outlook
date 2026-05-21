# ingest-outlook

A Claude Code skill that connects Microsoft 365 (Outlook mail, calendar, Teams meetings) to a local Obsidian-style vault, with **autonomous self-healing**: when something breaks, the user just says "no funciona" and the system diagnoses and repairs itself.

- **Reads**: Outlook mail, calendar events (own + shared), Teams meetings with transcripts
- **Writes**: send mail, create/update/delete calendar events
- **Self-heals**: token refresh, scope drift, file permissions, missing dirs all get fixed automatically
- **Privacy**: everything runs locally, no third-party services, OAuth tokens stored at mode 0600
- **Audit**: every write action logged to `~/.config/ingest-outlook/audit.jsonl`
- **Stdlib only**: Python 3.10+, no `pip install` required

## Quick start

```bash
git clone https://github.com/danilobrando/ingest-outlook.git
cd ingest-outlook
./install.sh
```

The installer copies the skill files to `~/.claude/skills/ingest-outlook/`, verifies your Python version, and prints the remaining configuration steps.

After that:

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

## What you can ask your Claude Code agent

Once installed, you don't run commands directly. You talk to your agent:

| You say | Agent does |
|---|---|
| "Trae mis correos de hoy" / "fetch today's mail" | Runs `fetch.py mail` and writes to your vault |
| "Qué reuniones tengo mañana?" | Runs `fetch.py calendar` and shows you |
| "Mándale un correo a X" | Runs `fetch.py send-mail` (with a confirmation step) |
| "Agéndame una llamada con Y el jueves a las 3" | Runs `fetch.py event-create` |
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

Two scripts:
- **`fetch.py`** — talks to Microsoft Graph (OAuth2 + REST). 10 subcommands. Stdlib only.
- **`ingest.py`** — normalizes Graph JSON responses into vault markdown. Stdlib only.

Single contract file:
- **`SKILL.md`** — read by Claude Code at session start. Defines trigger phrases, auto-recovery policy, and how the agent should react to user-reported problems.

## Subcommands

```
fetch.py mail --scope <folder|query> --scope-kind <folder|query> --days N --vault-root <path>
fetch.py calendar [--calendar-id <id>] --days N --vault-root <path>
fetch.py meetings --days N --vault-root <path>            # corporate accounts only
fetch.py list-calendars                                    # own + shared
fetch.py send-mail --to "x@y.com" --subject "..." --body "..." [--cc, --bcc, --html, --dry-run]
fetch.py event-create --subject "..." --start ISO --end ISO [--timezone, --attendees, --body, --location]
fetch.py event-update --event-id <id> [--subject, --start, --end, --add-attendees, --body, --location, --cancel --yes]
fetch.py event-delete --event-id <id> --yes
fetch.py doctor [--vault-root <path>]                      # read-only diagnostic
fetch.py fix [--vault-root <path>] [--quiet]               # diagnose + auto-repair
fetch.py version
```

All commands accept `--verbose` for debug output to stderr.

## Self-healing

The connector's defining feature. When something goes wrong, the user runs ONE command (or, with the SessionStart hook, runs nothing at all):

```bash
python ~/.claude/skills/ingest-outlook/fetch.py fix
```

`fix` runs 9 diagnostic checks (env vars, config dir, token freshness, file permissions, network, auth, scope drift, vault writable, log/audit files writable). For each failing check, it either auto-repairs or prints the exact manual command needed. Then it re-verifies.

| Failure mode | Auto-fix? |
|---|---|
| Config directory missing | Yes (`mkdir -p`) |
| Token file mode wrong (not 0o600) | Yes (`chmod 600`) |
| Token expired but refresh available | Yes (already automatic at every call) |
| Token rejected by Graph | Yes (delete + re-OAuth, opens browser) |
| Scope drift | Yes (re-OAuth with full scope set) |
| Vault directory missing | Yes (`mkdir -p`) |
| Log/audit files not writable | Yes |
| Env var `MS_GRAPH_CLIENT_ID` missing | No (cannot edit shell env from a child process); prints exact command |
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
- **Token storage**: `~/.config/ingest-outlook/token.json` at file mode `0o600` (owner-only read/write).
- **Audit log**: every write action (send-mail, event-create/update/delete) recorded with timestamp, recipients, subject, body hash (not body content) to `~/.config/ingest-outlook/audit.jsonl`.
- **Read PII safeguards**: ingested mail bodies are truncated to 500 chars in the vault. Calendar bodies to 400 chars. Transcripts are stored verbatim (the whole point).
- **Sensitive paths warning**: do not let `~/.config/` be synced by cloud providers (iCloud, OneDrive, MDM profiles). The connector's `fix` command detects most cases.

## Required Microsoft Graph permissions

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

## License

MIT. See [`LICENSE`](LICENSE).
