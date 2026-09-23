# Azure app registration walkthrough

You need to register a Microsoft Entra (Azure AD) app to give `ingest-outlook` permission to talk to Microsoft Graph on your behalf. This is a **one-time setup**, takes about 5 minutes, and is free (no Azure subscription required for app registrations, though you may need an Azure account).

The result is two GUIDs you will paste into your shell env:
- `MS_GRAPH_CLIENT_ID` — the Application (client) ID
- `MS_GRAPH_TENANT_ID` — the Directory (tenant) ID, or `common` for personal accounts

---

## Personal vs corporate

| | Personal Microsoft account | Corporate Microsoft 365 tenant |
|---|---|---|
| Where you register | [portal.azure.com](https://portal.azure.com) signed in with your personal MSA, OR [entra.microsoft.com](https://entra.microsoft.com) | Same portals, but signed in with your corporate account |
| Account types | "Personal Microsoft accounts only" OR "Personal + work/school" | "Single tenant" (recommended) |
| `MS_GRAPH_TENANT_ID` | `common` (default) or `consumers` | the tenant GUID |
| Admin consent | Not required for any scope | Required for `OnlineMeetingTranscript.Read.All`; the rest are user-level |
| Teams access | Not available | Available |

If you're installing this for someone else's corporate account, **send them the [request template](#corporate-installs-request-template) below** to forward to their IT admin. The admin registers the app on their behalf and returns the two GUIDs.

---

## Corporate tenant, read-only (for the IT admin)

Use this profile when company governance requires the connector to read only the signed-in employee's own mailbox and calendar.

1. In the company's Microsoft Entra tenant, register a **single-tenant** app: "Accounts in this organizational directory only".
2. Under **Authentication**, add the **Mobile and desktop applications** platform with this redirect URI exactly:

   ```text
   http://localhost:8765/callback
   ```

3. Set **Allow public client flows** to **Yes**. Do not create a client secret.
4. Under **API permissions**, add only these Microsoft Graph **Delegated** permissions:

   - `User.Read`
   - `Mail.Read`
   - `Calendars.Read`
   - `offline_access`

5. Click **Grant admin consent for <tenant>**.
6. Give users the **Application (client) ID** and **Directory (tenant) ID**. These are public identifiers, not secrets.

Do not add `Mail.Send`, `Calendars.ReadWrite`, `Calendars.Read.Shared`, `OnlineMeetings.Read`, or `OnlineMeetingTranscript.Read.All` for this profile. Shared/delegated mailboxes and calendars are outside its governance boundary. Teams transcripts are also outside the default read-only profile.

If the company operates two separate Entra tenants, register one single-tenant app in each tenant. The alternative is a multitenant app with separate admin consent in every tenant.

---

## Step-by-step registration

### 1. Sign in to the Azure portal

Open [portal.azure.com](https://portal.azure.com) or [entra.microsoft.com](https://entra.microsoft.com) in an **Incognito/Private** window (avoids cached sessions tied to old tenants).

Sign in with the Microsoft account you want the connector to act on behalf of (personal or corporate).

### 2. Open App registrations

Navigate to: **Microsoft Entra ID** → **App registrations** → **New registration**.

### 3. Fill in the registration form

| Field | Value |
|---|---|
| **Name** | `ingest-outlook` (or any name you want; only you see it) |
| **Supported account types** | See below |
| **Redirect URI** | Leave blank in this step. You add it afterward. |

**Supported account types options:**

- For **personal** Microsoft accounts only (Hotmail/Outlook.com/Live):
  Choose `Personal Microsoft accounts only`.
- For **corporate** use:
  Choose `Accounts in this organizational directory only - Single tenant`.
- For **both** (multi-tenant + personal):
  Choose `Accounts in any organizational directory and personal Microsoft accounts`.

Click **Register**.

### 4. Configure the redirect URI

Once registered, you land on the app's Overview page. From the left sidebar, go to **Authentication** → **Add a platform** → **Mobile and desktop applications**.

In the redirect URI panel, **Custom redirect URI** field, enter exactly:

```
http://localhost:8765/callback
```

Click **Configure**.

### 5. Enable public client flows

Still on the **Authentication** page, scroll to **Advanced settings**. Set **Allow public client flows** to **Yes**. Click **Save** at the top of the page.

This is what enables the OAuth Authorization Code flow with PKCE to work without a client secret (correct for a native/desktop client).

### 6. Add API permissions

From the left sidebar, go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**.

Add the following (use the search box; each is its own entry):

**Always needed:**
- `User.Read`
- `offline_access`
- `Mail.Read`

**For sending mail:**
- `Mail.Send`

**For calendar:**
- `Calendars.ReadWrite`
- `Calendars.Read.Shared`

**For corporate Teams meetings (skip if personal account):**
- `OnlineMeetings.Read`
- `OnlineMeetingTranscript.Read.All` ← **this one requires admin consent**

Click **Add permissions**.

### 7. Grant admin consent (corporate, if requesting transcripts)

If you added `OnlineMeetingTranscript.Read.All`, click **Grant admin consent for [your tenant]** on the API permissions page. You must be a tenant admin to do this, OR ask your IT admin to do it for you.

The other scopes are user-level and consent at first OAuth runtime; no admin button needed.

### 8. Copy the IDs

Go back to the app's **Overview** page. Copy:

- **Application (client) ID** — paste this as `MS_GRAPH_CLIENT_ID` in your shell.
- **Directory (tenant) ID** — paste this as `MS_GRAPH_TENANT_ID` for corporate accounts. For personal accounts use `common` instead.

In your `~/.zshrc` (or `~/.bashrc`):

```bash
export MS_GRAPH_CLIENT_ID="paste-application-client-id-here"
export MS_GRAPH_TENANT_ID="common"   # OR the corporate tenant GUID
```

Then `source ~/.zshrc`.

### 9. First run

```bash
python3 ~/.claude/skills/ingest-outlook/fetch.py fix
```

Browser opens, you sign in, you grant the listed permissions, browser tab closes itself with a confirmation. The token is cached and refreshed automatically from here on.

Verify health:

```bash
python3 ~/.claude/skills/ingest-outlook/fetch.py doctor
```

All checks should be `[PASS]`. If anything is `[FAIL]` or `[WARN]`, run `fix` again (or follow the manual steps the output prints).

---

## Corporate installs: request template

If you're installing this for an employee whose Microsoft 365 tenant is managed by IT, the employee cannot register the app themselves. Send this to their IT admin (the employee can forward it):

> **Subject**: Request to register a native client application in Microsoft Entra ID
>
> Please register a public-client native application in our Entra ID tenant with the following configuration:
>
> - **Name**: `ingest-outlook` (or `Personal Knowledge Connector — <employee name>`)
> - **Type**: Public client / native (no client secret)
> - **Supported account types**: Single tenant
> - **Platform**: Mobile and desktop applications
> - **Redirect URI**: `http://localhost:8765/callback`
> - **Allow public client flows**: Yes
> - **API permissions** (all Delegated, Microsoft Graph):
>   - User.Read
>   - offline_access
>   - Mail.Read
>   - Calendars.Read
>
> Please grant admin consent for these four delegated permissions and share the resulting Application (client) ID and Directory (tenant) ID with me. These IDs are public identifiers, not secrets.
>
> The application is open-source ([repo URL]); it runs entirely on my local machine, stores its OAuth tokens in the user's local profile, and never sends data to any third-party server. The local read-only profile blocks write commands before any token or network request.
>
> Permissions I am explicitly NOT requesting (to make scope clear):
> - Mail.Send, Mail.ReadWrite, Calendars.ReadWrite, Calendars.Read.Shared, Calendars.ReadWrite.Shared, OnlineMeetings.Read, OnlineMeetingTranscript.Read.All, Files.Read.All, Sites.Read.All, ChannelMessage.Read.All, Chat.Read, or any Application (vs Delegated) permission.

---

## Troubleshooting registration

### "Sign-in failed: AADSTS5000225: This tenant has been blocked due to inactivity"

Your MSA has an old "Default Directory" Entra ID tenant that Microsoft auto-disabled (180 days of no activity). Two options:

1. Sign in with a different Microsoft account (one that has an active tenant).
2. Sign up for [Azure free tier](https://azure.microsoft.com/free) (no charges if you only use App registrations). This activates your tenant.

### "The ability to create applications outside of a directory has been deprecated"

Microsoft deprecated registering apps without an Entra ID tenant. You need a tenant. Sign up for Azure free, or join the Microsoft 365 Developer Program (eligibility-based).

### "End users cannot grant consent to newly registered multitenant apps without verified publishers"

This is a warning, not a blocker, when the app is set to multi-tenant. In practice it does NOT block the consent flow for your own MSA on the consumer tenant. If you do hit a blocker:
- Switch the app's Supported account types to "Personal accounts only" OR "Single tenant"
- OR add an MPN (Microsoft Partner Network) ID to verify the publisher

### "Redirect URI mismatch" during OAuth

The Authentication blade redirect URI must match exactly: `http://localhost:8765/callback` (no trailing slash, no `https`). If you ran the connector with a custom `INGEST_OUTLOOK_REDIRECT_PORT`, update Azure to match.
