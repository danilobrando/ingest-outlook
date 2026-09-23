# Investigación: transcripciones de Teams por Microsoft Graph (v0.6)

Fecha de consulta de todas las fuentes: **23-sep-2026**. Todas las fuentes son oficiales de Microsoft (`learn.microsoft.com`), salvo donde se indica lo contrario. `[V]` = confirmado literalmente en documentación oficial vigente. `[NV]` = no confirmado (falta evidencia oficial explícita, aunque el diseño no lo contradiga).

Este documento responde el Paso 0 de la tarea v0.6 y cierra con las implicaciones para el código de `sync`.

---

## 1. ¿Un invitado que no organizó la reunión puede listar y bajar transcripciones con permiso delegado `OnlineMeetingTranscript.Read.All`?

**[V] El endpoint existe y su tabla de permisos no lo restringe al organizador.**

```http
GET /me/onlineMeetings/{onlineMeeting-id}/transcripts
GET /users/{user-id}/onlineMeetings/{onlineMeeting-id}/transcripts
```

Tabla de permisos exacta de la operación **List transcripts**:

| Tipo de permiso | Mínimo privilegio | Privilegio mayor |
|---|---|---|
| Delegado (cuenta laboral/educativa) | `OnlineMeetingTranscript.Read.All` | No disponible |
| Delegado (cuenta personal) | No soportado | No soportado |
| Aplicación | `OnlineMeetingTranscript.Read.All`, `OnlineMeetingTranscript.Read.Chat` | No disponible |

> Cita: "Choose the permission or permissions marked as least privileged for this API. […] Delegated (work or school account) — OnlineMeetingTranscript.Read.All". [List transcripts](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-list-transcripts?view=graph-rest-1.0), ms.date 2025-12-02, consultado 23-sep-2026.

Contenido de la transcripción: `GET .../transcripts/{id}/content` o la URL `transcriptContentUrl` que trae el objeto `callTranscript`. Misma familia de permisos.

**Lo que SÍ está confirmado por contraste:** la función alternativa `getAllTranscripts` (que sí es explícitamente "todas las reuniones que el usuario organizó") **no admite permiso delegado en absoluto**:

| Tipo de permiso | Mínimo privilegio |
|---|---|
| Delegado (laboral/educativa) | **No soportado** |
| Aplicación | `OnlineMeetingTranscript.Read.All` |

> Cita: "Get transcripts from all online meetings that a user is an organizer of." + tabla "Delegated (work or school account): Not supported." [onlineMeeting: getAllTranscripts](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-getalltranscripts?view=graph-rest-1.0), ms.date 2026-08-13, consultado 23-sep-2026.

Esto importa porque descarta de raíz una implementación tentadora: `getAllTranscripts` sería más simple (una llamada trae todo lo que organizó la persona) pero **no funciona con el modelo delegado que usa v0.6** (OAuth PKCE, sin daemon). Solo sirve con permiso de aplicación + `application access policy`, que el contrato ya descartó.

**[NV] Que un asistente NO organizador consiga realmente un `200 OK` (y no un `403`) al llamar `List transcripts`.** Ninguna página de referencia de la API dice explícitamente "un asistente puede listar transcripciones de una reunión que no organizó"; solo dice que la tabla de permisos delegados no distingue el rol. La página hermana `Get onlineMeeting` sí lo dice explícitamente para el recurso `onlineMeeting` (ver §2): "These request URLs accept both the organizer's and the invited attendee's user token". Por diseño de la API es razonable esperar el mismo comportamiento en `List transcripts` (ambos cuelgan del mismo objeto `onlineMeeting` y de la misma familia de permisos), pero no hay una frase equivalente en la página de transcripciones. **Se prueba en vivo el jueves.**

---

## 2. `$filter=JoinWebUrl eq '...'` con permiso delegado para asistentes no organizadores; alternativa desde el evento de calendario

**[V] Confirmado, cita textual.**

```http
GET /me/onlineMeetings?$filter=JoinWebUrl eq '{joinWebUrl}'
GET /users/{userId}/onlineMeetings?$filter=JoinWebUrl eq '{joinWebUrl}'
```

Tabla de permisos (misma que `GET /me/onlineMeetings/{id}` por ID):

| Tipo de permiso | Mínimo privilegio | Privilegio mayor |
|---|---|---|
| Delegado (laboral/educativa) | `OnlineMeetings.Read` | `OnlineMeetings.ReadWrite` |
| Aplicación (solo variante `/users/{id}`) | `OnlineMeetings.Read.All` | `OnlineMeetings.ReadWrite.All` |

> Cita textual: **"These request URLs accept both the organizer's and the invited attendee's user token (delegated permission) or user ID (app permission)."** — inmediatamente debajo de los patrones `GET /me/onlineMeetings/{meetingId}` y `GET /users/{userId}/onlineMeetings/{meetingId}`, y la sección de `joinWebUrl` hereda la misma tabla de permisos. [Get onlineMeeting](https://learn.microsoft.com/en-us/graph/api/onlinemeeting-get?view=graph-rest-1.0), ms.date 2024-11-01 (actualizada 2026-08-03), consultado 23-sep-2026.

Esta es la frase más importante de todo el documento: **es la única confirmación explícita, con esas palabras, de que el modelo delegado no está limitado al organizador** — aplica al recurso `onlineMeeting` (que incluye la relación `transcripts`), aunque no está repetida palabra por palabra en la página de transcripciones (ver [NV] de §1).

**Alternativa para obtener el `onlineMeeting id` desde un evento de calendario** (que es el flujo real de `sync`, no el `JoinWebUrl` copiado a mano):

El recurso `event` de Outlook calendar trae la propiedad `onlineMeeting` (tipo `itemBody`-like con `joinUrl`, no confundir con `onlineMeetingUrl`, que Microsoft marca en vías de baja). El flujo es:

1. `GET /me/events/{id}?$select=onlineMeeting,isOnlineMeeting,onlineMeetingProvider` (o ya viene incluido en la respuesta de `calendarView` si no se recorta con `$select`) → trae `onlineMeeting.joinUrl`.
2. `GET /me/onlineMeetings?$filter=JoinWebUrl eq '<joinUrl codificado en URL>'` → una colección con **un** objeto `onlineMeeting`, que trae el `id` real.
3. `GET /me/onlineMeetings/{id}/transcripts` con ese `id`.

> Cita: "Access the URL to join a meeting using joinUrl, available via the onlineMeeting property of the event. Do not use the onlineMeetingUrl property of the event because onlineMeetingUrl will soon be deprecated." [Create or set an event as an online meeting](https://learn.microsoft.com/en-us/graph/outlook-calendar-online-meetings), consultado 23-sep-2026. **`joinWebUrl` debe ir URL-encoded en el `$filter`** (nota explícita en la página de `Get onlineMeeting`).

---

## 3. Nombre exacto y ruta en el Teams admin center; cmdlet de PowerShell

**[V] Confirmado, cita textual completa (fuente única y autorizada, ya coincide con la verificación previa del proyecto, §1.3).**

- Ruta exacta: **Teams admin center → Meetings → Meeting settings → sección "Transcript API access" → interruptor "Microsoft Graph access"**.
- Atribución de hablantes: dentro de la misma sección, botón **"Configure"** → interruptor **"Include speaker attribution"**.
- Ambos vienen **apagados por defecto**.

> Cita textual: "By default, Microsoft Graph access is off, so agents and apps can't access meeting transcripts, regardless of app-level permissions. […] 1. In the left navigation of the Teams admin center, go to Meetings > Meeting settings. 2. Under Transcript API access, turn the Microsoft Graph access toggle On […] 3. Select Configure, and then […] turn the Include speaker attribution toggle On. By default, this setting is off." [Manage transcript API access for Teams meetings](https://learn.microsoft.com/en-us/microsoftteams/meeting-transcript-api-access), ms.date 2026-07-23, consultado 23-sep-2026.

Cmdlet de PowerShell equivalente (mismo doc, sección "Using PowerShell"):

```powershell
Set-CsTeamsMeetingConfiguration -EnableGraphTranscriptAccess $true -EnableAttributedTranscripts $true -Identity Global
```

- `-EnableGraphTranscriptAccess $true` = "Microsoft Graph access" On.
- `-EnableAttributedTranscripts $true` = "Include speaker attribution" On.
- `-Identity Global` aplica a todo el tenant (no hay política granular por usuario documentada para este ajuste específico).

---

## 4. Scopes delegados mínimos

| Necesidad | Scope delegado | Consentimiento de admin |
|---|---|---|
| Todo el buzón (lectura) | `Mail.Read` | No requerido (nivel usuario) |
| Calendario propio (lectura) | `Calendars.Read` | **[V] No requerido** — cita: entrada "Calendars.Read" en la tabla de permisos, "Admin consent required: No". [Microsoft Graph permissions reference](https://learn.microsoft.com/en-us/graph/permissions-reference), consultado 23-sep-2026 |
| Resolver la reunión (metadatos, `JoinWebUrl`, id) | `OnlineMeetings.Read` | **[NV]** — no se confirmó la fila exacta en la tabla de referencia (página truncada en la consulta); el propio `docs/azure-app-setup.md` de este repo ya lo trata como scope de nivel usuario, sin exigir el botón de admin consent |
| Transcripciones | `OnlineMeetingTranscript.Read.All` | **[V] Sí, obligatorio** — confirmado en §1 y ya documentado en `docs/azure-app-setup.md` de este repo |
| Refresh sin reabrir navegador | `offline_access` | No requerido |
| Identificar la cuenta (`/me`) | `User.Read` | No requerido |

Set completo para el perfil `--teams` (igual al que ya fija el Paso 2 de la tarea):

```text
User.Read Mail.Read Calendars.Read offline_access OnlineMeetings.Read OnlineMeetingTranscript.Read.All
```

**Recomendación operativa para el equipo de TI** (Documento 2): aunque solo `OnlineMeetingTranscript.Read.All` exige el consentimiento de administrador de forma obligatoria, se pide **un solo "Grant admin consent for `<tenant>`"** que cubra las seis. Es un clic, evita que cada una de las 10 personas tenga que aceptar un cuadro de consentimiento individual la mañana del jueves, y es el mismo patrón que ya usa `docs/azure-app-setup.md`.

---

## 5. Formato del contenido: `$format=text/vtt`, etiquetas `<v Nombre>`, comportamiento sin atribución de hablantes

**[V] Confirmado.**

- `text/vtt` (WebVTT) es el formato **por defecto** y trae utterances con marca de tiempo y **etiqueta de voz `<v Speaker Name>`** (atribuido por hablante).
- `application/vnd.microsoft.graph.transcript+text` trae las mismas utterances con marca de tiempo pero **sin** información de hablante.
- El formato se elige con el header `Accept` o con el parámetro `$format` — para `text/vtt` ambos son equivalentes.
- **Si la atribución de hablantes está apagada en el tenant** ("Include speaker attribution" = Off) y la petición pide `text/vtt`, Graph devuelve **`403 Forbidden`** con `innerError.code = SpeakerAttributionNotAllowed`. La solución documentada es reintentar con `Accept: application/vnd.microsoft.graph.transcript+text` (sin nombres).

> Fuente: [Get callTranscript](https://learn.microsoft.com/en-us/graph/api/calltranscript-get?view=graph-rest-1.0), ms.date 2025-12-02 (actualizada 2026-07-08), consultado 23-sep-2026 (contenido verificado vía extracto indexado del propio Learn, sección "Get the content of a transcript").

Consecuencia directa para el parser del `.vtt` que pide el contrato (§7 de `system3-v0.2`, "una línea por intervención `[HH:MM:SS] Nombre Apellido: texto`"): **si el equipo de TI no activa "Include speaker attribution", el nombre siempre sale `Desconocido`** — no porque el parser falle, sino porque el propio VTT no trae la etiqueta `<v …>` (o, peor, si el código pide `text/vtt` a secas sin manejar el 403, la corrida entera de esa transcripción falla). El código debe:
1. Pedir siempre `Accept: text/vtt` primero.
2. Si la respuesta es `403 SpeakerAttributionNotAllowed`, reintentar automáticamente con `Accept: application/vnd.microsoft.graph.transcript+text` y marcar todos los hablantes como `Desconocido` (no es un error de la corrida, es un estado esperado hasta que el equipo de TI active el interruptor).

---

## 6. Límites de throttling relevantes de `/me/messages` para una primera corrida de 30 días (~1000 mensajes)

**[V], con una fuente compuesta.** La sección "Outlook service limits" de la página de límites por servicio no pudo citarse línea por línea porque la página completa excede el tamaño que la herramienta de consulta puede traer de una sola vez (es una página larga con decenas de servicios); los números siguientes se repitieron de forma consistente en dos consultas independientes contra el mismo documento oficial:

- **10.000 solicitudes por ventana de 10 minutos**, por combinación de app + buzón.
- Microsoft recomienda diseñar para **4–10 solicitudes por segundo** (el techo real es ~16–17/seg, pero no se debe diseñar al límite).
- **Máximo 4 solicitudes concurrentes** por buzón.
- Límite de subida (no aplica a `sync`, que es solo lectura): 15 megabits cada 30 segundos por buzón.
- Estos límites son **fijos, no configurables** ni por soporte ni por cuota.

> Fuente: [Microsoft Graph service-specific throttling limits](https://learn.microsoft.com/en-us/graph/throttling-limits), sección "Outlook service limits", consultado 23-sep-2026. Comportamiento general de reintento (recomendado por Microsoft: respetar el header `Retry-After` en cualquier `429`, no reintentar de inmediato) confirmado en [Microsoft Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling), ms.date 2025-01-14, consultado 23-sep-2026.

**Implicación numérica para el backfill de 30 días / ~1000 mensajes:** una corrida de `sync` que pagina `/me/messages` con `$top` razonable (25–50) hace entre 20 y 40 solicitudes para 1000 mensajes, muy por debajo de las 10.000/10min y de las 4 concurrentes (el código de v0.6 no lanza pedidos en paralelo contra el mismo buzón). El riesgo real no es el throttling de Graph — es el tope de 8 minutos por corrida y `max_messages_per_run` que ya fija el contrato, ambos pensados para el ancho de la ventana de :00–:10.

---

## Implicaciones para el código

Orden recomendado de llamadas en `sync --only meetings` (Paso 2.5 de la tarea):

1. Recorrer los eventos de la ventana pasada (`calendar_past_days` hacia atrás) que tengan `isOnlineMeeting: true` y `onlineMeetingProvider` de Teams. Pedir el evento con `$select` que incluya `onlineMeeting` para tener `joinUrl` sin una llamada extra.
2. `GET /me/onlineMeetings?$filter=JoinWebUrl eq '<joinUrl URL-encoded>'` → si la colección viene vacía, registrar en el log y seguir con el siguiente evento (reunión sin metadatos de Teams, cancelada, o expirada — el recurso expira 60 días después del inicio/fin). No es un error fatal.
3. Con el `id` de `onlineMeeting`: `GET /me/onlineMeetings/{id}/transcripts`. Manejar así los códigos de error:
   - **`403 GraphAccessToTranscriptsDisabled`**: El equipo de TI no activó "Microsoft Graph access". Registrar en `ingesta.log` con ese motivo exacto (no genérico "403"), **no** detener correo ni calendario (regla ya fijada en el contrato §5 del Paso 2 de la tarea).
   - **`403` en el `/content` con `innerError.code = SpeakerAttributionNotAllowed`**: reintentar con `Accept: application/vnd.microsoft.graph.transcript+text`, hablantes → `Desconocido`.
   - **`404`**: la reunión no tiene transcripción (nunca se transcribió, o política `AllowTranscription` apagada) — no es error, se omite en silencio.
   - **Colección vacía en el paso 2 (`onlineMeetings` sin resultados)**: probable invitado sin acceso al recurso, evento no asociado a una reunión real de Teams creada por API, o el evento ya expiró. Registrar y continuar.
4. Por cada transcripción nueva (id no visto en `<perfil>/ids-reuniones.txt`): pedir el contenido con `Accept: text/vtt` primero; solo si falla con `SpeakerAttributionNotAllowed`, repetir sin atribución.

**Riesgo a validar el jueves en vivo, con cuentas reales de prueba (no productivas)**: confirmar que una cuenta invitada (no organizadora) efectivamente recibe `200 OK` en el paso 3 y no un `403` distinto a `GraphAccessToTranscriptsDisabled`. Si el equipo de TI ya activó el interruptor del tenant y aun así una cuenta no organizadora recibe `403`, el motivo más probable —no cubierto por ninguna doc oficial encontrada— sería una política de reunión (`CsTeamsMeetingPolicy`) que restrinja el acceso a la transcripción a organizador/co-organizador a nivel de la reunión misma, no del tenant. Eso quedaría **[NV]** hasta que se reproduzca.
