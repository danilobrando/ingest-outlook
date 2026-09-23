# Instalación en Windows, perfil corporativo de solo lectura

## Antes de empezar

Necesitas:

- Git for Windows.
- Python 3.10 o posterior desde [python.org](https://www.python.org/downloads/windows/), instalado con **Install for me only** y **Add python.exe to PATH** marcado.
- Los valores **Application (client) ID** y **Directory (tenant) ID** que te entrega TI. Son identificadores públicos, no contraseñas ni secretos.
- Un vault en `%USERPROFILE%\cerebros\<cargo>\`.

Desactiva los alias de la Microsoft Store para Python: **Configuración > Aplicaciones > Configuración avanzada de aplicaciones > Alias de ejecución de aplicaciones**. Apaga `python.exe` y `python3.exe` cuando apunten a la Store.

## Instalar

Abre PowerShell. Reemplaza `<cargo>`, `<client-id>` y `<tenant-id>`, y ejecuta estos tres comandos:

```powershell
git clone --branch v0.5.1 https://github.com/danilobrando/ingest-outlook.git "$env:LOCALAPPDATA\ingest-outlook"
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\ingest-outlook\install.ps1" -VaultRoot "$env:USERPROFILE\cerebros\<cargo>" -ClientId "<client-id>" -TenantId "<tenant-id>" -ReadOnly
py -3 "$env:USERPROFILE\cerebros\<cargo>\.claude\skills\ingest-outlook\fetch.py" fix
```

El primer comando descarga el conector en una carpeta de tu usuario, fuera del vault. El segundo copia al vault solo lo necesario (sin `.git`, pruebas ni CI) en `<vault>\.claude\skills\ingest-outlook` y guarda la configuración en `%USERPROFILE%\.config\ingest-outlook`, que nunca entra al vault ni al respaldo. Si tu vault está en `C:\cerebros\<cargo>` (perfiles con tilde), usa esa ruta en `-VaultRoot` y en el tercer comando.

El tercer comando abre el navegador. Inicia sesión con tu cuenta corporativa y acepta el acceso. El conector queda limitado a lectura por configuración local y por los permisos concedidos por TI.

Si `py -3` no existe, usa `python`. Si Windows abre la Microsoft Store, revisa los alias de ejecución indicados arriba.

## Actualizar

```powershell
git -C "$env:LOCALAPPDATA\ingest-outlook" fetch --tags
git -C "$env:LOCALAPPDATA\ingest-outlook" checkout <nueva-versión>
```

Luego vuelve a correr el segundo comando de instalación. Tu sesión y tu configuración se conservan.

## Usarlo con Claude Code

Pídele directamente:

- "Trae mis correos de la carpeta Cerebro de los últimos 7 días".
- "¿Qué reuniones tengo mañana?". El agente usa `calendar --days 1 --ahead 1` para cubrir desde hoy a las 00:00 hasta mañana a las 23:59:59, en la hora local.

En perfil de solo lectura Claude no debe ofrecer enviar correos ni crear, cambiar o borrar eventos.

## Recomendación de gobierno

Ingiere una carpeta dedicada de Outlook, por ejemplo `Cerebro`, en vez de todo el buzón. La operación equivalente es:

```text
mail --scope Cerebro --scope-kind folder
```

Crea `Cerebro` en el **primer nivel** del buzón (clic derecho sobre el nombre de tu cuenta > Nueva carpeta), no dentro de Bandeja de entrada: en esta versión el conector solo encuentra carpetas de primer nivel. Mueve a esa carpeta únicamente los mensajes que deban entrar al vault. No uses buzones ni calendarios compartidos o delegados.

## Errores comunes

### Need admin approval

El administrador de TI todavía no dio consentimiento a los permisos delegados de la app. En Microsoft Entra debe abrir **API permissions** y seleccionar **Grant admin consent for <tenant>**. No intentes consentir una app de terceros ni agregues permisos de escritura.

### AADSTS50011

La URI de redirección no coincide. TI debe configurar la plataforma **Mobile and desktop applications** con exactamente `http://localhost:8765/callback`.

### AADSTS7000218

La app no está habilitada como cliente público. TI debe poner **Allow public client flows** en **Yes**. Esta app usa PKCE y no lleva client secret.
