# Respaldo: ingesta por hook de inicio de Claude Code

Usa esto **solo si** el Programador de tareas de Windows no te dejó crear la tarea "Cerebro - ingesta" (política de la empresa para usuarios estándar). Si la tarea programada sí se creó, no necesitas nada de este documento. En Mac no hace falta: `schedule install` crea un LaunchAgent de tu usuario (ver [`docs/instalacion-cerebro-es.md`](instalacion-cerebro-es.md#en-mac)).

## Qué cambia

Con el Programador de tareas, la ingesta corre sola cada 30 minutos, sin que nadie tenga la sesión de Claude Code abierta. Con este respaldo, la ingesta corre **solo cuando la persona abre Claude Code** (una vez por sesión, no cada 30 minutos). Si alguien no abre Claude Code en todo el día, ese día no hay ingesta nueva. Es peor que la tarea programada, pero es mejor que no tener nada.

## Cómo funciona

Claude Code soporta un evento `SessionStart` en `settings.json`: un comando que corre cada vez que arranca una sesión. **Ese comando bloquea el arranque de la sesión hasta que termina** — por eso no puede ser `sync --all` directamente (tardaría varios minutos y la persona se quedaría mirando la pantalla). La solución es que el comando del hook **lance** la sincronización en segundo plano y **regrese de inmediato**, sin esperar a que termine.

En Windows, eso se logra con `Start-Process`: PowerShell arranca `pythonw.exe` (la variante de Python sin ventana de consola) como un proceso aparte y no espera su resultado.

## `settings.json`

Agrega esto en `<vault>\.claude\settings.json` (créalo si no existe). Reemplaza `<vault>` por la ruta real y `<python>` por la ruta de tu `pythonw.exe` (normalmente junto al `python.exe` que ya resolviste en la instalación).

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "powershell -NoProfile -WindowStyle Hidden -Command \"Start-Process -WindowStyle Hidden -FilePath 'pythonw' -ArgumentList '\\\"<vault>\\\\.claude\\\\skills\\\\ingest-outlook\\\\fetch.py\\\" sync --all --vault-root \\\"<vault>\\\"'\"",
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

Notas sobre esta sintaxis, verificadas contra la documentación oficial de hooks de Claude Code ([code.claude.com/docs/en/hooks](https://code.claude.com/docs/en/hooks), consultado 23-sep-2026):

- `matcher: "startup"` limita el hook al arranque normal de sesión (no a `resume`, `clear` ni `compact`). Si prefieres que corra también al reanudar una sesión, usa `"*"` en vez de `"startup"`.
- `timeout` son los segundos que Claude Code espera antes de cancelar el comando del hook. Como el comando solo **lanza** el proceso y no espera su resultado, 15 segundos sobra de sobra — el propio `Start-Process` regresa en milisegundos.
- No existe un modo `async` para `SessionStart` en la versión actual de Claude Code: el hook siempre bloquea el arranque hasta que el comando termina. Por eso el diseño de arriba no es "correr sync en background con async: true" — es "correr un comando que termina rápido porque lo único que hace es lanzar otro proceso y no esperarlo".
- Alternativa más legible, si el JSON con comillas anidadas resulta frágil de mantener: usa la forma `command` + `args` (forma "exec"), que evita el parseo de shell. Ejemplo equivalente:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "powershell",
            "args": [
              "-NoProfile",
              "-WindowStyle", "Hidden",
              "-Command",
              "Start-Process -WindowStyle Hidden -FilePath pythonw -ArgumentList '\"<vault>\\.claude\\skills\\ingest-outlook\\fetch.py\" sync --all --vault-root \"<vault>\"'"
            ],
            "timeout": 15
          }
        ]
      }
    ]
  }
}
```

## Un hook por perfil, o uno para los dos

`sync --all` ya cubre todas tus cuentas (un perfil por empresa) bajo una sola toma del lock del vault, igual que la tarea programada. No hace falta un hook por perfil — un solo hook con `sync --all` es equivalente y más simple. Si por alguna razón necesitas separarlos (por ejemplo, para depurar cuál cuenta está fallando), agrega entradas dentro del mismo arreglo `SessionStart` con `sync --profile <empresa-1>` y `sync --profile <empresa-2>` en comandos distintos: cada uno toma el lock del vault por su cuenta (el segundo espera al primero), así que es seguro, pero son varias tomas del lock en vez de una, y por eso no es lo recomendado para uso normal.

## Verificar que quedó funcionando

Cierra y vuelve a abrir Claude Code en el vault. Espera unos 20–30 segundos y revisa:

```
<vault>\.claude\system3\logs\ingesta.log
```

Debe aparecer una línea nueva. Si no aparece nada, corre el comando de `Start-Process` directamente en PowerShell (sin el hook) para ver el error, y confirma que la ruta a `pythonw` y al script son correctas.
