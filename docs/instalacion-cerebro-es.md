# Instalar tu cerebro — una o varias cuentas de Microsoft 365

Esta guía instala **un vault** (tu cerebro) que lee **tus cuentas de Microsoft 365**: una por cada empresa con la que trabajas (una, dos o más). Cada cuenta es un **perfil** del conector. Sigue los pasos en orden. Cada bloque de comandos se copia y se pega completo, con Enter al final.

Los ejemplos usan dos empresas, `<empresa-1>` y `<empresa-2>`. Si tienes una sola, omite los pasos de la segunda; si tienes más, repítelos por cada una.

## Antes de empezar

Necesitas tener listo:

- **Git** instalado (si no lo tienes, dile a quien te está acompañando en la sesión).
- **Python 3.10 o posterior**, instalado desde [python.org](https://www.python.org/downloads/windows/) con **Install for me only** y **Add python.exe to PATH** marcados.
- **Los alias de la Microsoft Store apagados** para Python: **Configuración → Aplicaciones → Configuración avanzada de aplicaciones → Alias de ejecución de aplicaciones** → apaga `python.exe` y `python3.exe`.
- **Un par de identificadores por empresa que te entrega el equipo de TI**: un Client ID y un Tenant ID de `<empresa-1>`, y otro par de `<empresa-2>`. No son contraseñas — puedes tenerlos en un correo o un chat. El equipo de TI los obtiene con [`docs/ti-registro-app-es.md`](ti-registro-app-es.md).
- **El nombre corto de cada empresa** (su *slug*): minúsculas, sin tildes ni espacios, por ejemplo `acme`. Es la etiqueta `empresa:` que llevará cada nota.
- **La ruta de tu vault.** En Windows va a ser `%USERPROFILE%\cerebros\<tu-cargo>` (por ejemplo `C:\Users\maria\cerebros\finanzas`). Si tu usuario de Windows tiene tilde o eñe, usa en cambio `C:\cerebros\<tu-cargo>`. En Mac: `~/cerebros/<tu-cargo>` (ver la sección [En Mac](#en-mac) al final).

En todos los comandos de abajo, reemplaza:

- `<vault>` por la ruta completa de tu vault.
- `<empresa-1>`, `<empresa-2>` por el nombre corto de cada empresa.
- `<client-id-empresa-1>`, `<tenant-id-empresa-1>` por los valores de TI para `<empresa-1>`.
- `<client-id-empresa-2>`, `<tenant-id-empresa-2>` por los valores de TI para `<empresa-2>`.

## Paso 1 — Descargar el conector

```powershell
git clone --branch v0.6.0 https://github.com/danilobrando/ingest-outlook.git "$env:LOCALAPPDATA\ingest-outlook"
```

Esto descarga el conector una sola vez, fuera del vault. No lo vuelvas a correr salvo para actualizar.

## Paso 2 — Instalar el perfil de la primera empresa

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ingest-outlook\install.ps1" -VaultRoot "<vault>" -Profile <empresa-1> -Empresa <empresa-1> -Layout cerebro -ClientId "<client-id-empresa-1>" -TenantId "<tenant-id-empresa-1>" -ReadOnly -Teams
```

## Paso 3 — Instalar el perfil de la segunda empresa (y programar la ingesta automática)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ingest-outlook\install.ps1" -VaultRoot "<vault>" -Profile <empresa-2> -Empresa <empresa-2> -Layout cerebro -ClientId "<client-id-empresa-2>" -TenantId "<tenant-id-empresa-2>" -ReadOnly -Teams -Schedule
```

`-Schedule` se agrega **solo en el último comando** (con una sola empresa, agrégalo en el Paso 2). Programa una sola tarea de Windows llamada **"Cerebro - ingesta"**, que corre todas tus cuentas seguidas cada 30 minutos (a las :00 y a las :30), de 7:00 a. m. a 7:00 p. m., de lunes a viernes. No hace falta programar nada más.

## Paso 4 — Iniciar sesión con cada cuenta

```powershell
py -3 "<vault>\.claude\skills\ingest-outlook\fetch.py" fix --profile <empresa-1>
```

Se abre el navegador. Inicia sesión con tu **cuenta de `<empresa-1>`** y acepta los permisos que pide (correo, calendario, reuniones — todos de solo lectura). Cuando el navegador confirme, vuelve a la terminal.

Repite con la segunda empresa:

```powershell
py -3 "<vault>\.claude\skills\ingest-outlook\fetch.py" fix --profile <empresa-2>
```

Esta vez inicia sesión con tu **cuenta de `<empresa-2>`**. Es un inicio de sesión distinto por cada cuenta — no repitas la misma cuenta en dos perfiles.

## Paso 5 — Probar sin escribir nada todavía

```powershell
py -3 "<vault>\.claude\skills\ingest-outlook\fetch.py" sync --all --dry-run
```

Este comando revisa todas tus cuentas y te dice qué escribiría, pero no toca el vault ni guarda ninguna marca de progreso. Si termina sin errores, ya puedes correr la ingesta real quitando `--dry-run` (o simplemente esperar a que corra sola con la tarea programada del Paso 3).

## Paso 6 — Verificar que quedó funcionando

```powershell
py -3 "<vault>\.claude\skills\ingest-outlook\fetch.py" schedule status
```

Te muestra la última y la próxima corrida de la tarea "Cerebro - ingesta", y el resultado de la última vez que corrió.

También puedes abrir directamente el registro (la carpeta `.claude` no se ve en Obsidian; ábrelo desde el Explorador de archivos o el Bloc de notas):

```
<vault>\.claude\system3\logs\ingesta.log
```

Cada línea es una corrida de una cuenta. El estado al final de la línea significa:

| Estado | Qué significa |
|---|---|
| `ok` | Corrió bien, trajo lo que había nuevo |
| `saltada` | Otro paso (extracción o ingesta) tenía el turno; no pasó nada raro, la siguiente corrida sigue normal |
| `requiere-login` | Tu sesión con esa cuenta venció; hay que volver a iniciar sesión (ver abajo) |
| `error` | Algo falló; la línea trae un motivo corto |

Lo que llega queda en `<vault>\raw\entradas\correo\<empresa>\…`, `<vault>\raw\entradas\calendario\<empresa>\…` y `<vault>\raw\entradas\reuniones\<empresa>\…`.

## Qué hacer si...

### La línea dice `requiere-login`

Tu token de acceso venció y no se pudo renovar solo. Corre de nuevo el Paso 4 para esa cuenta:

```powershell
py -3 "<vault>\.claude\skills\ingest-outlook\fetch.py" fix --profile <empresa-1>
```

(o el perfil que diga la línea del registro). Se abre el navegador otra vez, inicias sesión, listo.

### La línea dice `saltada: lock ocupado`

Es normal. Dos procesos (la ingesta y la extracción a proyectos) no pueden escribir el vault al mismo tiempo, así que uno de los dos espera su turno. La siguiente corrida, 30 minutos después, sigue sin perder nada. No hay que hacer nada.

### Aparece "Need admin approval" al iniciar sesión

El equipo de TI todavía no dio el consentimiento de administrador para esa cuenta en ese tenant. No lo intentes resolver tú mismo aceptando algo distinto: avísale al equipo de TI que falta el paso "Grant admin consent" en Entra para esa empresa. Mientras tanto, las otras cuentas pueden seguir funcionando normal.

### Error `AADSTS50011`

La dirección de redirección no coincide. Es un problema de configuración de TI, no tuyo: avísale al equipo de TI que la app debe tener exactamente `http://localhost:8765/callback` como redirect URI, bajo "Mobile and desktop applications".

### Error `AADSTS7000218`

La app no está marcada como cliente público. Avísale al equipo de TI que en **Authentication → Advanced settings** falta poner **Allow public client flows** en **Yes**. Esta app no usa contraseña de aplicación (client secret); si TI intentó crear una, no la necesitas y puede ignorarla.

### La tarea programada no se pudo crear (política de la empresa lo bloquea)

Si el Paso 3 termina con un mensaje de que Windows no permite crear tareas programadas para tu usuario, no te quedas sin ingesta: existe un respaldo que corre la sincronización cada vez que abres Claude Code en lugar de cada 30 minutos. Sigue las instrucciones de [`docs/respaldo-hook-inicio.md`](respaldo-hook-inicio.md).

### Python abre la Microsoft Store en vez de correr

Repite el chequeo de "Antes de empezar": los alias de ejecución de `python.exe`/`python3.exe` deben estar apagados en Configuración → Aplicaciones → Alias de ejecución de aplicaciones. Si después de apagarlos `py -3` sigue sin funcionar, usa `python` en su lugar en todos los comandos de esta guía.

## En Mac

El vault va en `~/cerebros/<tu-cargo>`. Necesitas Python 3.10+ (`python3 --version`). En Terminal:

```bash
git clone --branch v0.6.0 https://github.com/danilobrando/ingest-outlook.git ~/.local/share/ingest-outlook
F=~/.local/share/ingest-outlook/fetch.py
python3 "$F" configure --profile <empresa-1> --empresa <empresa-1> --layout cerebro --client-id "<client-id-empresa-1>" --tenant-id "<tenant-id-empresa-1>" --read-only --teams --vault-root ~/cerebros/<tu-cargo>
python3 "$F" configure --profile <empresa-2> --empresa <empresa-2> --layout cerebro --client-id "<client-id-empresa-2>" --tenant-id "<tenant-id-empresa-2>" --read-only --teams --vault-root ~/cerebros/<tu-cargo>
python3 "$F" fix --profile <empresa-1>
python3 "$F" fix --profile <empresa-2>
python3 "$F" sync --all --dry-run
python3 "$F" schedule install --vault-root ~/cerebros/<tu-cargo>
python3 "$F" schedule status --vault-root ~/cerebros/<tu-cargo>
```

`schedule install` crea un agente de tu usuario (`~/Library/LaunchAgents/com.cerebro.ingesta.<código>.plist`) que corre `sync --all` cada 30 minutos mientras tu sesión de macOS esté abierta (a diferencia de Windows, no se limita a lunes-viernes 7:00–19:00). Para quitarlo: `python3 "$F" schedule remove --vault-root ~/cerebros/<tu-cargo>`. El registro queda en `~/cerebros/<tu-cargo>/.claude/system3/logs/ingesta.log`, igual que en Windows.

## Privacidad — léelo antes de instalar

Este conector va a leer **todo tu buzón** (menos correo no deseado, elementos eliminados y borradores) de cada cuenta, tu calendario, y las transcripciones de las reuniones de Teams en las que participes. Eso incluye información que normalmente clasificarías como sensible.

**Hasta que la gerencia de cada empresa firme la autorización de tratamiento de datos, ningún dato real se envía al modelo de IA.** El conector escribe los datos crudos en tu vault igual que siempre — eso no cambia — pero el paso que los analiza y arma proyectos automáticamente los deja retenidos, marcados como pendientes, hasta que exista esa autorización por escrito. No necesitas hacer nada para que esto funcione así: es el comportamiento por defecto mientras la bandera de autorización no esté activada.
