---
name: ingest-outlook
description: Outlook/Microsoft 365 connector for Windows, macOS, and Linux, with a recommended corporate read-only profile plus optional read-write capabilities AND autonomous self-healing. Subcommands - configure, mail/calendar/meetings/list-calendars (read), sync (non-interactive ingestion into the cerebro layout, one profile per Microsoft 365 tenant via --profile), schedule (automatic sync: Windows task or macOS LaunchAgent), send-mail/event-create/event-update/event-delete (write profile only), doctor/fix (recovery), version. **sync exit code 4 means a person must sign in again: ask the user to run `fix --profile <perfil>` (it opens the browser); automations must never open a browser.** **AUTO-RECOVERY TRIGGER: when the user reports ANY issue related to Outlook, Microsoft 365, correo, mail, email, calendario, calendar, reuniones, meetings, or Teams — including vague phrases like "no funciona", "no me llegan correos", "está raro", "no veo mis reuniones", "no puedo enviar", "error con el mail", "algo no anda con mi outlook" — this skill MUST be invoked FIRST and the orchestrator MUST silently run `fetch.py fix --vault-root <user-vault>` BEFORE responding to the user. The agent parses the fix output and either confirms the auto-repair OR walks the user through the manual steps conversationally.** Read subcommands write to the configured Outlook output directory, idempotent per day. In read-only mode, write subcommands are unavailable and the agent must never offer them.
---

# ingest-outlook, Outlook + Microsoft 365 connector

Ingests Outlook mail, calendar events, and Teams meetings (with transcripts) into the vault as markdown the graphify pipeline can read and the rest of the AI Brain Starter substrate can act on. **Also acts as the autonomous recovery layer**: when the user reports anything wrong with their M365 setup, this skill silently self-diagnoses and self-repairs before bothering the user.

Same family as ingest-gmail, ingest-slack, ingest-linear. Same output contract under `External Inputs/<Source>/...`. The only architectural difference is that there is no Microsoft Graph MCP, so the data fetch runs locally through Microsoft Graph REST. The orchestrator pipes `fetch.py` stdout into `ingest.py` stdin.

## Runtime paths and interpreter

The skill base directory is the directory Claude Code reports when it loads this skill. Refer to it below as `<skill-base>`; do not assume `~/.claude/skills` or any other hardcoded location.

- macOS/Linux: use `python3`.
- Windows: use `python` or `py -3`, whichever resolves to Python 3.10+. Never use a `python3`/`python` executable whose path contains `WindowsApps`, because that is the Microsoft Store stub.
- Replace `<python>` and `<skill-base>` in every example with those resolved values.

## AUTO-RECOVERY POLICY (read this first)

**The user does not need to know commands, file paths, or technical terms. They never type `fetch.py fix`. They just say something is wrong with their email/calendar/Teams, and you (the orchestrator) take care of it.**

### When to auto-trigger

If the user's message contains ANY signal that something is wrong with their Microsoft 365 / Outlook / Teams environment, your **first action — before any clarifying question, before any other tool call — is to run**:

```bash
<python> "<skill-base>/fetch.py" fix [--vault-root <user-vault-root>]
```

Run this silently. Do NOT show the user the command. Do NOT explain you're "running diagnostics". Just do it and react to the output.

### Trigger phrases (not exhaustive; treat broadly)

In Spanish and English:
- "no funciona", "no me funciona", "está fallando", "no jala", "no anda"
- "no me llegan correos", "no recibo mails", "no veo nuevos emails"
- "no puedo enviar", "no me deja enviar", "no responde al enviar"
- "no veo mi calendario", "el calendario está vacío", "no aparecen mis reuniones"
- "no veo la reunión de X", "falta una reunión"
- "no carga", "carga lento", "se quedó pegado", "se cuelga"
- "error con [outlook/correo/calendario/teams]"
- "algo raro con mi [mail/calendario/reuniones]"
- "perdí acceso", "me sacó la sesión", "me pide login otra vez"
- "no veo nada nuevo desde hace [tiempo]"
- ANY question that implies the M365 ingestion pipeline is broken

When in doubt, run `fix`. It's idempotent and cheap. False positives cost zero. False negatives leave the user stuck.

### How to interpret the `fix` output

`fix` exits 0 or 1, and prints three sections: diagnostics, addressed issues, re-verification.

**Exit 0 + "Nothing to fix"**:
- The connector is healthy.
- The user's reported problem is NOT in the connector. Possible causes: Outlook app itself is misbehaving, Microsoft service outage, user expectation mismatch (e.g., "no me llegó el correo" but the sender hasn't sent it yet).
- Respond: "Tu conector con Microsoft 365 está sano. ¿Podés contarme más sobre qué no funciona? Por ejemplo: ¿el correo no llegó? ¿no podés abrirlo? ¿está en Outlook directamente o tampoco lo ves en tu vault?"

**Exit 0 + "System healthy. Auto-fixed N issue(s)"**:
- The connector had issues that were auto-repaired (token refresh, perms, dir, etc).
- Respond: "Listo, encontré un par de cosas que no estaban bien y las arreglé. Probá de nuevo lo que estabas haciendo." (Mention what was fixed at a high level — "tu sesión se había vencido, ya re-autenticé" — without technical jargon like "token" or "OAuth".)

**Exit 1 + manual steps remaining**:
- Something needs the user to do an action.
- Read the "Manual steps" output for each failing check.
- Translate to conversational Spanish, ONE step at a time. Don't dump a wall of shell commands.
- Wait for confirmation between steps. After the user confirms they did the steps, re-run `fix` to verify.

### How to translate manual steps for non-technical users

The `fix` output is concise and technical. You translate.

Bad (technical dump):
> "Run: echo 'export MS_GRAPH_CLIENT_ID=\"...\"' >> ~/.zshrc"

Good (conversational + concrete):
> "Faltó configurar una variable importante. Te paso el comando exacto, abrí Terminal (Cmd+Space → Terminal) y pegá esto, después dale Enter:
>
> ```
> echo 'export MS_GRAPH_CLIENT_ID="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"' >> ~/.zshrc
> source ~/.zshrc
> ```
>
> Cuando lo hagas, decime y verifico que todo quedó bien."

### What NOT to do

- Do NOT ask "what command should I run?" — you already know: `fix`
- Do NOT ask the user to read log files or send screenshots
- Do NOT mention `fetch.py`, `doctor`, `tokens`, `OAuth`, `scopes`, `Graph API` unless the user specifically asks how it works
- Do NOT tell the user "contact your administrator" without first running `fix` (the manual steps from `fix` already include who to contact when relevant)
- Do NOT show the raw `fix` output. Translate it.

### After successful recovery

If `fix` resolved the issue, gently confirm what was happening (in plain language) and proactively offer to re-run the user's last action:

> "Tu sesión con Microsoft se había vencido y la renové. Si querés, vuelvo a traer tus correos más recientes ahora."

### Hard escalation

Only after `fix` has been run AND its manual steps have been followed AND the issue persists, surface that this is beyond auto-recovery:

> "Probé reparar todo lo que el conector puede arreglar solo, y seguimos con un problema en [nombre del check]. Esto necesita que [el admin de tu organización / Danny Bravo] mire. Te puedo armar el resumen para mandárselo."

Then provide a concise problem summary (NOT a log dump) ready to forward.

## When to use

- User says `/ingest-outlook mail <folder-or-query> [--days N]`
- User says `/ingest-outlook calendar [--days N] [--ahead N]`
- User says `/ingest-outlook meetings [--days N]` (corporate accounts only)
- User asks to capture, sync, ingest, or pull Outlook mail, calendar, or Teams meetings into the vault

Do NOT use for:
- Non-Outlook sources (Gmail, Slack, Linear get their own connectors)
- Teams channel chat messages (out of scope; this skill covers mail, calendar, and meeting transcripts)
- Bulk operations (the skill is single-action; for bulk send/modify, wrap multiple calls and confirm each)

## Capabilities by subcommand

| Subcommand | Read/Write | Scope required | Output |
|---|---|---|---|
| `mail` | Read | `Mail.Read` | Vault file under configured output `/Mail/<folder>/<date>.md` |
| `calendar --days N [--ahead 0..60]` | Read | `Calendars.Read` (own). Shared calendars only when `shared_calendars` is enabled. Read-only without that flag sees own calendars only | Vault file under configured output `/Calendar/<date>.md` |
| `meetings` | Read | `OnlineMeetings.Read`, `OnlineMeetingTranscript.Read.All` (only when an admin enabled `teams`) | Vault file under configured output `/Meetings/<date>.md` |
| `list-calendars` | Read | `Calendars.Read`; shared entries require `shared_calendars` (`Calendars.Read.Shared`). Without that, read-only mode lists own calendars only | Human-readable list on stdout |
| `send-mail` | **Write** | `Mail.Send` | Confirmation line on stdout |
| `event-create` | **Write** | `Calendars.ReadWrite` | Event id + subject on stdout |
| `event-update` | **Write** | `Calendars.ReadWrite` | Updated event id + changed fields on stdout |
| `event-delete` | **Write** | `Calendars.ReadWrite` | Confirmation line on stdout, requires `--yes` |
| `sync` | Read | `Mail.Read`, `Calendars.Read` (+ Teams scopes with `teams`) | Cerebro raw files under `raw/entradas/...` + one ingest-log line per profile; exit 4 = needs sign-in |
| `schedule` | Local only | none | Windows task / macOS LaunchAgent that runs `sync --all` |

### Read-only profile contract

When the effective configuration has `read_only: true`, `send-mail`, `event-create`, `event-update`, and `event-delete` do not exist as capabilities for the agent. Do not offer, suggest, simulate, or attempt those commands, including with `--dry-run`. The connector enforces this locally before requesting a token, but the agent must also honor the policy at planning time. Do not access shared or delegated mailboxes/calendars unless `shared_calendars` was explicitly enabled by the operator.

In the read-only profile without `shared_calendars`, `list-calendars` and `calendar` only see the signed-in user's own calendars. They do not list or read calendars that were shared with that user.

`meetings` exists only when an administrator enabled Teams for this connector (`teams: true`, which needs admin consent). In the read-only profile without Teams, do not offer or attempt `meetings`.

Exit code 3 means the organization read-only policy blocked the command before any Microsoft call. It is not a connector error, a bad login, or something `fix` can repair. Explain that to the user as a company policy: the action is not allowed here. Do not describe it as a failure or a bug.

## Cerebro layout: profiles, `sync` and `schedule` (v0.6)

A "cerebro" is one vault per person that reads one or more Microsoft 365 accounts, one **profile** per company tenant. Profiles are generic slugs chosen at install time (`--profile <slug>`), never hardcoded; a vault can have 1..N of them.

- **Profile state** lives outside the vault in `<config-dir>/<slug>/` (config, token, logs, `state.json` watermark, `ids-correo.txt` / `ids-reuniones.txt` indices, `needs-login` marker). Select it with `--profile <slug>` before or after the subcommand, or `INGEST_OUTLOOK_PROFILE`.
- **Setup**: `configure --profile <slug> --empresa <slug> --layout cerebro --client-id <guid> --tenant-id <guid> --read-only [--teams] --vault-root "<vault>"`. `--empresa` is mandatory for the cerebro layout: it is written in every raw file and names the `<empresa>` folder.
- **Raw files** (vault-relative, configurable with `--raw-root`, default `raw/entradas`):
  - `raw/entradas/correo/<empresa>/YYYY-MM-DD/HHMM-<subject-slug>.md`, one per message, immutable, full plain-text body (100 KB cap), attachment names only;
  - `raw/entradas/calendario/<empresa>/YYYY-MM-DD.md`, one per day from yesterday to +14 days, rewritten each run (only when something changed);
  - `raw/entradas/reuniones/<empresa>/YYYY-MM-DD-<meeting-slug>.md` plus the original `.vtt`, immutable, one line per intervention `[HH:MM:SS] Nombre: texto` (only with `teams`).
  Frontmatter keys are Spanish (`fuente, empresa, cuenta, fecha, id_origen, participantes, asunto, proyecto`, plus `carpeta, hilo, adjuntos, ingestado` for mail). `participantes` items are `Nombre <correo>` (or `<correo>`), lowercase emails, deduplicated, sender or organizer first, rooms excluded. The connector never writes the `sintetico` key.
- **`sync`** (`sync --all` for every cerebro profile of the vault, or `sync --profile <slug>`; `--dry-run` writes nothing; `--only mail|calendar|meetings`) takes the single vault lock (`.claude/system3/cerebro.lock` by default), processes profiles alphabetically under one lock, stops cleanly after 8 minutes (the next run continues from the watermark), and appends one line per profile to `.claude/system3/logs/ingesta.log`: `ISO | perfil | correo N nuevos | calendario D días | reuniones M | estado ok|saltada|requiere-login|error: motivo`.
- **Exit codes of `sync`**: 0 ok or skipped (lock busy), 1 error, 2 configuration problem, **4 needs sign-in**. With `--all` the worst code wins; one profile failing never stops the others.
- **Exit 4 / `requiere-login`**: the refresh token no longer works. `sync` never opens a browser (it runs from a scheduled task or a hook with nobody watching). Tell the user, in plain words, that their session with that company expired and ask them to sign in; with their OK run `<python> "<skill-base>/fetch.py" fix --profile <perfil>` (this is the one step that opens the browser). `fix` clears the `needs-login` marker. Do not retry `sync` in a loop and do not try to open the browser from any automation.
- **Teams**: a `403 GraphAccessToTranscriptsDisabled` in the log means the tenant switch "Microsoft Graph access" (Teams admin center) is off. It is an IT setting, not a connector bug; mail and calendar keep working. Without "Include speaker attribution", speakers show as `Desconocido`.
- **`schedule install|remove|status`**: Windows registers `\Rewired\Cerebro - ingesta` (current user, only while signed in, no elevation, Mon-Fri 07:00-19:00 every 30 min, `pythonw.exe fetch.py sync --all`). If policy denies it (exit 5), the fallback is the Claude Code startup hook in `docs/respaldo-hook-inicio.md`. macOS installs a per-user LaunchAgent `com.cerebro.ingesta.<hash>` (every 30 min). Other systems: message only.
- **Lock**: one lock per vault shared with the extraction and the s3 index (`cerebro_lock.py acquire|release|status`). `sync` refuses to run if profiles of the same vault, or the vault's `.claude/system3/config.json`, disagree on `lock_path`.

## Personal vs corporate Microsoft accounts

| | Personal (Office 365 Home/Personal) | Corporate (Microsoft 365 Business) |
|---|---|---|
| `MS_GRAPH_TENANT_ID` | `common` (default) or `consumers` | `<tenant-uuid>` from IT admin |
| Mail | Available | Available |
| Calendar | Available | Available |
| Teams meetings + transcripts | **Not available** | Available only when `teams: true` (requires admin consent on `OnlineMeetingTranscript.Read.All`) |
| Recommended read-only scopes | `User.Read Mail.Read Calendars.Read offline_access` | `User.Read Mail.Read Calendars.Read offline_access` |

The `meetings` subcommand prints a warning and returns zero results on personal tenants.

## PII awareness, read this before running

Outlook content is the highest-PII surface in the vault. Mail carries personal email addresses, phone numbers, full names, contract numbers, billing details, internal company memos, and (for corporate accounts) HR/legal/financial material. Transcripts capture full verbatim conversation.

Operator obligations:

1. **Treat the output files as confidential.** Never commit them to a public repository. Never paste them into a public chat. Never share them outside the vault owner.
2. **Never ingest a shared mailbox you do not own.** Delegated mailboxes, shared inboxes, and "On behalf of" access scenarios require the inbox owner's explicit consent.
3. **Scrub before sharing.** If a file ever needs to leave the vault, scrub names, addresses, phone numbers, account numbers, and message bodies first.
4. **Mail bodies are truncated to 500 chars** in the legacy layout as a volume cap, not a redaction. The cerebro layout (`sync`) stores the **full plain-text body up to 100 KB** per message, because the extraction needs it; treat those files as confidential. Calendar bodies are truncated to 400 chars. **Transcripts are stored verbatim** because truncating defeats the purpose of capturing meeting content. Treat transcripts as the most sensitive output of this skill.
5. **Tokens stay local.** OAuth tokens live in the configured state directory (default `~/.config/ingest-outlook/token.json`). POSIX uses mode 0600; Windows relies on the user-profile ACL. Do not commit, copy, or share that file.

If any of these obligations is unclear, do not run the skill. Ask first.

## Prerequisites (one-time setup)

Before the first run:

1. Register a Microsoft Entra (Azure AD) app at https://portal.azure.com.
2. Under "Authentication", add a "Mobile and desktop applications" redirect URI of `http://localhost:8765/callback`. Enable "Allow public client flows".
3. Under "Supported account types", choose:
   - For personal use: "Personal Microsoft accounts only" (or "Accounts in any organizational directory and personal Microsoft accounts")
   - For corporate use: "Accounts in this organizational directory only (Single tenant)"
4. Under "API permissions", add delegated Microsoft Graph permissions:
   - Recommended read-only profile: only `User.Read`, `Mail.Read`, `Calendars.Read`, `offline_access`
   - Optional read-write profile: add `Mail.Send` and use `Calendars.ReadWrite`
   - Optional shared calendars: add `Calendars.Read.Shared`
   - Optional corporate Teams transcripts: add `OnlineMeetings.Read`, `OnlineMeetingTranscript.Read.All` (the second requires admin consent)
5. Copy the Application (client) ID and (for corporate) the Directory (tenant) ID. Export them:

   ```bash
   export MS_GRAPH_CLIENT_ID="<application-client-id>"
   export MS_GRAPH_TENANT_ID="common"                       # personal
   # export MS_GRAPH_TENANT_ID="<tenant-uuid>"              # corporate
   ```

   On macOS/Linux, add them to `~/.zshrc` so they persist. A starter template is in `<skill-base>/.env.example`. On any platform, prefer the persistent config command:

   ```text
   <python> "<skill-base>/fetch.py" configure --client-id <guid> --tenant-id <guid-or-alias> --vault-root "<vault-path>" --read-only
   ```

   Use `configure --show` to inspect each effective value and whether it came from an argument, environment variable, config file, or default.

The first run will open the browser to complete OAuth authorization. Subsequent runs use a cached refresh token. To force re-authorization (e.g. after scope changes), delete `~/.config/ingest-outlook/token.json`.

## How it works

The skill is a thin orchestrator. Two Python scripts do the work in the legacy layout:

- `<skill-base>/fetch.py` runs OAuth2 + Microsoft Graph queries, emits JSON to stdout
- `<skill-base>/ingest.py` reads the JSON, writes the vault file

The cerebro layout needs no pipe: `fetch.py sync` writes the raw files itself (see "Cerebro layout" above).

The subcommand is the first positional arg to `fetch.py`:

```bash
<python> "<skill-base>/fetch.py" <mail|calendar|meetings> \
    [subcommand-specific args] \
    --days N \
    --vault-root "<path>" \
  | <python> "<skill-base>/ingest.py"
```

### Subcommand: mail

```bash
<python> "<skill-base>/fetch.py" mail \
    --scope "<folder-name-or-query>" \
    --scope-kind <folder|query> \
    --days N \
    --vault-root "<vault-path>" \
  | <python> "<skill-base>/ingest.py"
```

Resolves the scope as a mail folder (`displayName eq <scope>` against `/me/mailFolders`) or a free-text `$search` query. Output: `External Inputs/Outlook/Mail/<scope-slug>/<YYYY-MM-DD>.md`.

### Subcommand: calendar

```bash
<python> "<skill-base>/fetch.py" calendar \
    --days N \
    --ahead N \
    --vault-root "<vault-path>" \
  | <python> "<skill-base>/ingest.py"
```

Pulls `/me/calendarView` from the start of the local day `(today - days + 1)` through the end of the local day `(today + ahead)`. `--ahead` defaults to `0` and accepts `0..60`. For "¿qué reuniones tengo mañana?", use `calendar --days 1 --ahead 1`, which covers today at 00:00 local through tomorrow at 23:59:59 local. Includes recurring instances expanded. Output: `External Inputs/Outlook/Calendar/<YYYY-MM-DD>.md`.

### Subcommand: meetings

```bash
<python> "<skill-base>/fetch.py" meetings \
    --days N \
    --vault-root "<vault-path>" \
  | <python> "<skill-base>/ingest.py"
```

For each calendar event with `onlineMeeting.joinUrl`, resolves the `onlineMeeting` object, lists transcripts, and downloads transcript content as VTT. Output: `External Inputs/Outlook/Meetings/<YYYY-MM-DD>.md`.

`meetings` is available only if an administrator enabled Teams (`teams: true`). Read-only mode without Teams exits 3 before requesting a token. Tell the user that is company policy, not an error.

Personal Microsoft accounts: the subcommand prints a warning and emits a zero-meeting payload. The vault file is still written for idempotency.

## Voice rules

- No em dashes (use commas, colons, periods, parentheses)
- No exclamation marks
- Direct, no fluff
- Sender, organizer, and subject quoted verbatim from Microsoft Graph
- Mail body excerpts: first 500 chars verbatim, then truncated with `[...truncated]`
- Calendar body excerpts: first 400 chars verbatim, then truncated
- Transcripts: stored verbatim in VTT format inside a `vtt` fenced code block

## Invocation

When invoked:

1. Parse the subcommand and arguments.
2. For `mail`: detect scope kind. If the scope is wrapped in quotes, contains whitespace, or uses Outlook KQL operators (`from:`, `to:`, `subject:`, `hasAttachment:`, `received:`), treat it as a query. Otherwise treat it as a folder name.
3. Omit `--vault-root` when it is present in effective configuration. Otherwise use the user's vault root; ask if unknown.
4. Run the pipeline:

   ```bash
   <python> "<skill-base>/fetch.py" <sub> [args] --days N [--vault-root "<vault-path>"] \
     | <python> "<skill-base>/ingest.py"
   ```

5. Surface the summary line printed by `ingest.py`.

If `MS_GRAPH_CLIENT_ID` is unset, `fetch.py` prints a clear setup error and exits non-zero. Do not write a stub file.

## Output contracts

### Mail

```yaml
---
type: external-input
source: outlook
kind: mail
folder_or_query: <verbatim>
scope_kind: folder | query
date_range: <YYYY-MM-DD>..<YYYY-MM-DD>
message_count: <int>
ingested_at: <ISO 8601>
entity_ids:
  outlook:
    - <message-id-1>
---
```

### Calendar

```yaml
---
type: external-input
source: outlook
kind: calendar
date_range: <YYYY-MM-DD>..<YYYY-MM-DD>
event_count: <int>
ingested_at: <ISO 8601>
entity_ids:
  outlook_event:
    - <event-id-1>
---
```

### Meetings

```yaml
---
type: external-input
source: outlook
kind: meetings
date_range: <YYYY-MM-DD>..<YYYY-MM-DD>
meeting_count: <int>
transcript_count: <int>
ingested_at: <ISO 8601>
entity_ids:
  outlook_event:
    - <event-id-1>
---
```

## Idempotency

Re-running any subcommand on the same calendar day overwrites the same vault file. No append. The `entity_ids` array reflects exactly the items in the current window.

## Acceptance test

A successful run produces:
1. One new (or refreshed) file under `External Inputs/Outlook/<Kind>/...`
2. A stdout summary line, e.g. `Wrote 7 message(s) to <path>` / `Wrote 3 event(s) to <path>` / `Wrote 2 meeting(s), 2 transcript(s) to <path>`

If the scope resolves but contains zero items in the window, write the file anyway with count `0` so re-runs are still idempotent.

## Proactive auto-recovery (session-start hook)

The strongest form of self-healing is **preventive**: run `fix --quiet` automatically every time the user's Claude Code session starts, before they even notice a problem. Tokens about to expire get refreshed, missing perms get repaired, scope drift gets re-consented, all silently. If anything truly needs attention, a single line lands in stderr that the agent can surface conversationally.

### Recipe for the user's `~/.claude/settings.json`

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "<python> \"<skill-base>/fetch.py\" fix --quiet || true"
          }
        ]
      }
    ]
  }
}
```

What this does:

- Every Claude Code session start, runs `fetch.py fix --quiet`.
- `--quiet` suppresses normal "Step 1/3..." output. If everything is healthy, the hook is silent.
- If `fix` auto-repairs something, it emits a one-line summary to stderr (`ingest-outlook fix: auto-repaired 2 (token, token-perms)`). The agent sees it and can mention it casually if relevant.
- If `fix` finds something it can't auto-repair, it emits a one-line warning and the agent surfaces it: "Hey, hay algo con tu Outlook que necesita atención: ..."
- The `|| true` at the end ensures a fix failure never blocks session start.

### Result for the user

When the user opens Claude Code on Monday morning:

- Token expired over the weekend → silently refreshed before they do anything
- Permissions drifted → silently restored
- New scope needed → agent proactively says "I need to re-authenticate you to gain access to X. Can I open the browser?"

The user never sees a "command line", never reads a log, never debugs anything.

## Revoke-on-demand procedure

If the user loses their laptop, suspects a token compromise, or wants to revoke the connector's access for any reason:

1. **Disconnect from Microsoft side (does not require the laptop):**
   - Sign in to [https://myapps.microsoft.com](https://myapps.microsoft.com) (corporate) or [https://account.microsoft.com/privacy/app-access](https://account.microsoft.com/privacy/app-access) (personal)
   - Find the registered app (named when you registered it in Azure) in the app list
   - Click "Remove" / "Revoke access"
   - All cached refresh tokens for this app are immediately invalidated
   
2. **Clean up the laptop (if accessible):**
   ```bash
   rm -rf ~/.config/ingest-outlook/
   ```
   Removes the token cache, log.jsonl, audit.jsonl, and lock file.

3. **Audit what the connector did (before revoking):**
   ```bash
   cat ~/.config/ingest-outlook/audit.jsonl | jq -s 'group_by(.action) | map({action: .[0].action, count: length})'
   ```
   Or open in any JSON viewer. Each write action (send-mail, event-create/update/delete) is recorded with timestamp, recipients, subject, body hash.

4. **If the laptop is compromised, also rotate the Azure app**:
   - portal.azure.com → Microsoft Entra ID → App registrations → the app → Delete
   - Create a fresh one with a new client ID (the old one is now permanently revoked)
   - Re-deploy the connector with the new client ID

## Portability across machines

This skill is a single directory under `~/.claude/skills/ingest-outlook/`. To port to another machine:

1. Copy the folder (`SKILL.md`, `fetch.py`, `ingest.py`, `sync_cerebro.py`, `cerebro_lock.py`, `schedule_win.py`, `schedule_mac.py`, `.env.example`; `docs/` is optional).
2. Ensure Python 3.10+ is installed. No pip dependencies.
3. Copy `.env.example` to `~/.zshrc` or a sourced shell file, fill in the values.
4. Run any subcommand once to trigger the OAuth flow; the token cache is created automatically.

For a corporate install, set `MS_GRAPH_TENANT_ID` to the corporate tenant UUID (from your IT admin). The default scopes upgrade automatically when `MS_GRAPH_TENANT_ID` is not `common` or `consumers`.

## Recovery: when something fails, run `fix`

The connector ships a self-healing entry point. **When ANYTHING is wrong**, run:

```bash
<python> "<skill-base>/fetch.py" fix
```

What it does:
1. **Diagnose**: runs all the same checks as `doctor` (env, config dir, token freshness, file permissions, network reachability, auth, scope drift, vault writable, log/audit files writable).
2. **Auto-repair**: for each issue, attempts an automatic fix. Examples:
   - Missing config directory → creates it
   - Wrong token file permissions → `chmod 600`
   - Token expired without refresh, or rejected by Graph, or missing scopes → triggers a fresh OAuth browser flow
   - Vault directory missing → creates it
3. **Print manual steps**: for issues that cannot be auto-fixed (env var missing, network down, conditional access policy block), prints the exact commands or actions the user must take. No vague "contact support" messages.
4. **Re-verify**: re-runs all checks. Reports final state and what (if anything) still needs manual action.

`fix` is idempotent: safe to run repeatedly. After a successful run, the system is in a known-good state.

Every error message printed by the connector ends with a hint pointing to `fix`. The user does not need to debug; they run `fix` and the script tells them what to do.

## Diagnostic-only: `doctor`

`doctor` runs the same checks as `fix` but does NOT attempt any repair (read-only). Use when you want to inspect state without changing anything. If `doctor` finds any failure, it prints "To auto-repair, run: ... fix" at the end.

## Failure modes (handled by `fix`)

- `MS_GRAPH_CLIENT_ID` unset: cannot auto-fix; `fix` prints the exact `export` command to add to `~/.zshrc`.
- Token store unreadable, expired without refresh, or rejected by Graph: `fix` deletes the bad token and triggers fresh OAuth.
- Token file permissions wrong: `fix` auto-chmods to 0o600.
- Config dir missing: `fix` auto-creates.
- Scope drift (token has scope X, app no longer registered for it; or app gained new scopes not yet consented): `fix` triggers a fresh OAuth with full scope set; if Microsoft still doesn't grant a scope, prints Azure portal steps to add it.
- Network unreachable: cannot auto-fix; `fix` prints troubleshooting steps (check connection, check proxy).
- Mail folder name not found: `fetch.py mail` lists available folders and exits 2 (no `fix` needed; just retry with a real folder name).
- Mail `$search` returns zero results: empty payload, `ingest.py` writes a `message_count: 0` file. Not a failure.
- Calendar/meetings window has zero items: empty payload, vault file still written for idempotency.
- Teams not available on personal tenant: `meetings` subcommand warns + emits empty payload.
- Read-only profile without Teams: `meetings` exits 3 (`blocked_read_only`) before requesting a token. That is organization policy, not a connector error. Do not run `fix` to "repair" it.
- Read-only profile without `shared_calendars`: `calendar` and `list-calendars` see only the user's own calendars.
- Exit code 3 from a write command or from `meetings`: explain the company policy. Do not treat it as a bug.
- Transcript not available for a meeting: that meeting's transcript array is empty; meeting still in output.
- HTTP 429 throttling: respects `Retry-After`, retries up to 3 times.
- AADSTS error codes (50158/50173/70008/65001/90094/50076/etc.): the `_post_token` handler maps known codes to a human-readable next action.
