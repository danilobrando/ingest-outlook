# Para el equipo de TI: registrar el conector en Entra + Teams admin center

Esto se hace **una vez en cada tenant, para cada empresa** cuyas cuentas va a leer el cerebro. Si hay varias empresas (cada una con su Microsoft 365), el procedimiento es idéntico y se repite en cada tenant. Al final de cada uno obtienes dos identificadores (no son contraseñas) que le entregas a quien instala los cerebros.

Tiempo estimado: 10 minutos por tenant.

---

## Qué es esto

El conector (`ingest-outlook`) es un programa que corre en el computador de cada persona, con la sesión de esa persona, y lee **su propio** correo, calendario y transcripciones de Teams para armar su vault de Obsidian. No es un servicio en la nube, no tiene servidor propio, y no puede actuar en nombre de nadie más que la persona que inició sesión. Por eso el registro es una app **de cliente público** (sin secreto), con permisos **delegados** (nunca "de aplicación").

---

## Parte 1 — Registrar la app en Microsoft Entra

Repite esto en el tenant de **cada empresa**, con la sesión de admin de ese tenant.

### 1. Registrar la app

En [entra.microsoft.com](https://entra.microsoft.com) de ese tenant: **Identity → Applications → App registrations → New registration**.

| Campo | Valor |
|---|---|
| Name | `Cerebro <Empresa> - lectura` (por ejemplo `Cerebro Acme - lectura`) |
| Supported account types | **Accounts in this organizational directory only — Single tenant** |
| Redirect URI | Déjalo en blanco aquí, se agrega en el paso siguiente |

Clic en **Register**.

### 2. Configurar el redirect URI

En la app recién creada: **Authentication → Add a platform → Mobile and desktop applications**.

En el campo de URI de redirección personalizada, escribe exactamente:

```
http://localhost:8765/callback
```

Clic en **Configure**.

### 3. Habilitar el flujo de cliente público

En la misma página **Authentication**, baja hasta **Advanced settings** y pon **Allow public client flows** en **Yes**. Clic en **Save**.

**No crees un client secret.** Esta app usa PKCE (Proof Key for Code Exchange); un secreto en un binario que corre en el computador de cada persona sería un secreto que cualquiera podría extraer, así que Microsoft recomienda no usarlo para este tipo de cliente y el conector no lo soporta.

### 4. Agregar permisos de Microsoft Graph — SOLO estos seis, todos delegados

**API permissions → Add a permission → Microsoft Graph → Delegated permissions.** Busca y agrega, uno por uno:

- `User.Read`
- `offline_access`
- `Mail.Read`
- `Calendars.Read`
- `OnlineMeetings.Read`
- `OnlineMeetingTranscript.Read.All`

Clic en **Add permissions**.

**No agregues ningún otro permiso.** En particular, no agregues (verificado contra la investigación en `docs/teams-transcripts-research.md`):

- `Mail.Send`, `Mail.ReadWrite`
- `Calendars.ReadWrite`, `Calendars.Read.Shared`, `Calendars.ReadWrite.Shared`
- `Files.Read.All`, `Sites.Read.All`, `Chat.Read`, `ChannelMessage.Read.All`
- Ningún permiso bajo la pestaña **Application permissions** (esta app nunca actúa sin un usuario con sesión abierta)

### 5. Dar consentimiento de administrador

Todavía en **API permissions**, clic en **Grant admin consent for `<nombre del tenant>`** → confirmar.

De los seis permisos, solo `OnlineMeetingTranscript.Read.All` exige obligatoriamente este paso (los otros cinco los podría aceptar cada persona individualmente en su primer inicio de sesión). Se piden los seis de una vez para no obligar a cada persona a aceptar un cuadro de consentimiento distinto el día de la instalación — un solo clic tuyo reemplaza todas esas interrupciones.

### 6. Copiar los dos identificadores

En **Overview** de la app, copia:

- **Application (client) ID**
- **Directory (tenant) ID**

Guárdalos para la Parte 3. Repite toda la Parte 1 en el tenant de cada empresa antes de continuar.

---

## Parte 2 — Teams admin center (ajustes a nivel de tenant)

Repite también en cada tenant, con sesión de admin de Teams.

### 1. Transcripción automática en las reuniones del equipo

En [admin.teams.microsoft.com](https://admin.teams.microsoft.com): **Meetings → Meeting policies** → la política que aplica a las personas que tendrán cerebro (normalmente **Global (Org-wide default)** si nadie tiene una política personalizada). Confirma que **Allow transcription** esté **On**. Esto ya suele venir activo por defecto; solo verifícalo.

### 2. Acceso de Microsoft Graph a las transcripciones — apagado por defecto desde julio de 2026

**Meetings → Meeting settings → sección "Transcript API access"** → interruptor **"Microsoft Graph access"** → **On**.

Sin este interruptor, el conector recibe `403 GraphAccessToTranscriptsDisabled` en cada intento de leer una transcripción, sin importar qué tan bien esté configurada la app de la Parte 1.

### 3. Atribución de hablantes (nombres en la transcripción) — también apagado por defecto

En la misma sección, clic en **Configure** → interruptor **"Include speaker attribution"** → **On**.

Sin este interruptor, la transcripción sale sin nombres (el conector la recibe igual, pero cada línea queda como "Desconocido" en vez del nombre de quien habló).

### Equivalente en PowerShell (si prefieres scriptear los dos interruptores)

```powershell
Set-CsTeamsMeetingConfiguration -EnableGraphTranscriptAccess $true -EnableAttributedTranscripts $true -Identity Global
```

Requiere el módulo `MicrosoftTeams` de PowerShell y sesión de admin (`Connect-MicrosoftTeams`). `-Identity Global` aplica a todo el tenant; no hay una política granular por usuario documentada para este par de ajustes.

**Nota de tiempos:** ambos cambios (Entra y Teams admin center) pueden tardar unos minutos en propagarse. Si el día de la instalación alguien recibe `403` inmediatamente después de que actives el interruptor, espera 10–15 minutos y reintenta antes de reportarlo como falla.

**[NV]** No se encontró documentación oficial que confirme, con esos ajustes ya activos, si una persona que **solo fue invitada** a una reunión (no la organizó) puede efectivamente leer su transcripción, o si hace falta además que sea coorganizadora. El diseño de la API no lo restringe explícitamente, pero conviene probarlo en vivo con una cuenta de prueba antes de confiar en el flujo para todas las personas. Ver el detalle en `docs/teams-transcripts-research.md`, sección 1.

---

## Parte 3 — Qué entregarle a quien instala los cerebros

Por cada tenant, uno de estos dos datos (no son secretos, se pueden mandar por correo o Slack sin cifrar):

| Tenant | Application (client) ID | Directory (tenant) ID |
|---|---|---|
| `<empresa-1>` | `<pegar aquí>` | `<pegar aquí>` |
| `<empresa-2>` | `<pegar aquí>` | `<pegar aquí>` |

Se usan así, un perfil por empresa, en el instalador de cada persona (ver [`docs/instalacion-cerebro-es.md`](instalacion-cerebro-es.md)):

```powershell
-Profile <empresa-1> -Empresa <empresa-1> -ClientId "<client id de empresa-1>" -TenantId "<tenant id de empresa-1>"
-Profile <empresa-2> -Empresa <empresa-2> -ClientId "<client id de empresa-2>" -TenantId "<tenant id de empresa-2>"
```

---

## Qué NO hacer

- No crear un client secret ni un certificado para esta app.
- No agregar ningún permiso de tipo **Application** (los que dicen "actúa sin un usuario con sesión").
- No agregar `Mail.Send`, `Calendars.ReadWrite`, ni ningún permiso de escritura: el conector v0.6 en este despliegue es de solo lectura y nunca llama endpoints de escritura, pero si el permiso existiera en la app, cualquier otro programa firmado con las mismas credenciales podría usarlo.
- No usar una `application access policy` (`New-CsApplicationAccessPolicy`) — eso es exclusivo del modelo de permiso de aplicación, que este conector no usa.
- No registrar la app como multi-tenant. Cada empresa tiene la suya, de tipo "Single tenant".
- No compartir el consentimiento de administrador de un tenant con otro: son registros de app independientes, uno por Microsoft 365.

## Cómo revocar el acceso

Si hay que cortar el acceso del conector — a una persona, o a todas — sin esperar a que expire un token:

- **Una persona puntual:** Entra ID → Users → esa persona → **Sign-in logs** no revoca nada por sí solo; para forzar que su sesión del conector pida credenciales de nuevo, revoca sus tokens de refresco: **Users → esa persona → Revoke sessions**. El conector detecta el rechazo en su siguiente intento y pide `fix --profile <empresa>` de nuevo.
- **Todo el tenant, de una vez:** Entra ID → **App registrations** → la app (`Cerebro <Empresa> - lectura`) → **Delete**. Esto invalida todos los tokens emitidos para esa app en ese tenant, para todas las personas a la vez, de inmediato.
- **Quitar un solo permiso sin borrar la app:** **API permissions** → ícono de basura junto al permiso → **Remove permission** → confirmar. Los tokens ya emitidos con el scope anterior siguen sirviendo hasta que expiran (normalmente 60–90 minutos); para cortar de inmediato, combina esto con revocar sesiones.

## Checklist final (una vez por cada empresa)

**`<empresa>`**

- [ ] App `Cerebro <Empresa> - lectura` registrada, single-tenant
- [ ] Redirect URI `http://localhost:8765/callback` configurado bajo "Mobile and desktop applications"
- [ ] "Allow public client flows" = Yes
- [ ] Los 6 permisos delegados agregados (ninguno de aplicación)
- [ ] "Grant admin consent for `<tenant>`" hecho
- [ ] Client ID y Tenant ID copiados y entregados a quien instala los cerebros
- [ ] Teams admin center → Meeting settings → "Microsoft Graph access" = On
- [ ] Teams admin center → "Include speaker attribution" = On
- [ ] Verificado: alguien corrió `fix --profile <empresa>` y el chequeo `auth` salió `PASS`

## Cómo verificar que quedó bien (sin tener que confiar en la palabra de nadie)

Pídele a cualquiera de las personas, después de que tenga el conector instalado ([`docs/instalacion-cerebro-es.md`](instalacion-cerebro-es.md)), que corra:

```powershell
py -3 "$env:LOCALAPPDATA\ingest-outlook\fetch.py" fix --profile <empresa-1>
```

(y lo mismo con el perfil de cada empresa). La salida debe mostrar el chequeo `auth` en `PASS`. Si sale `FAIL` con un mensaje que menciona "admin approval" o "consent", significa que el paso 5 de la Parte 1 (Grant admin consent) no se completó en ese tenant. Si el chequeo de correo/calendario pasa pero las reuniones fallan con un motivo que menciona transcripciones, revisa la Parte 2 (los dos interruptores de Teams admin center).
