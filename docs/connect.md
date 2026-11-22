# Guía de conexión a Google Ads

Cada usuario habilita Google Ads API en su propio proyecto de Google Cloud, descarga un JSON OAuth de tipo **Desktop app** y autoriza su cuenta en el navegador. Instalá primero el paquete desde [PyPI](https://pypi.org/project/google-ads-mcp-server/).

## Atajo: modo demo sin credenciales

Con el paquete instalado, podés probar dos cuentas sintéticas desde Claude Desktop:

```sh
google-ads-mcp-server register --client claude-desktop --demo
```

Reiniciá el cliente y pedile que liste las cuentas de Google Ads. Si después querés conectar una cuenta real en ese mismo cliente, eliminá primero la entrada demo `google_ads_mcp` de su configuración.

## 1. Habilitar la API y el acceso del proyecto

1. Creá o seleccioná un proyecto en [Google Cloud Console](https://console.cloud.google.com/).
2. Habilitá la [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com) en ese proyecto.
3. Para consultar cuentas reales, solicitá **Explorer** o un nivel superior en la página **Overview** de Google Ads API. El nivel **Test** solo permite consultar cuentas de prueba. Google asigna el [nivel de acceso al proyecto de Google Cloud](https://developers.google.com/google-ads/api/docs/api-policy/access-levels), que debe ser el mismo donde creás el cliente OAuth.

## 2. Configurar OAuth y descargar el JSON

1. Abrí **Google Auth Platform** y completá **Branding** con el nombre de la app, el correo de soporte y el contacto del desarrollador.
2. En **Audience**, elegí **External** para cuentas personales de Google. Si dejás la app externa en **Testing**, agregá en **Test users** el correo con acceso a Google Ads o al MCC que vas a usar.
3. En **Data Access → Add or remove scopes**, agregá `https://www.googleapis.com/auth/adwords` y guardá.
4. En **Clients**, creá un cliente de tipo **Desktop app** y pulsá **Download JSON**. Guardá el archivo en tu equipo.

En una app externa en **Testing**, la autorización para Google Ads vence siete días después del consentimiento. Ejecutá `connect` de nuevo para renovarla. Si vas a publicar tu app OAuth para otros usuarios, revisá los requisitos de verificación del permiso sensible. [Audiencia y modo de prueba](https://support.google.com/cloud/answer/15549945) · [Verificación de permisos sensibles](https://developers.google.com/identity/protocols/oauth2/production-readiness/sensitive-scope-verification)

## 3. Conectar la cuenta al cliente MCP

Elegí el comando que corresponda y reemplazá los IDs de ejemplo por los tuyos. Pegá la ruta del JSON cuando el CLI la pida. También podés pasarla como `--oauth-client "C:\Users\Tu Usuario\Downloads\client_secret.json"` en PowerShell.

```sh
# Cuenta directa
google-ads-mcp-server connect --client claude-desktop --customer 1234567890

# Una cuenta cliente bajo un MCC
google-ads-mcp-server connect --client codex --manager 9999999999 --customer 1234567890

# Todas las cuentas habilitadas bajo un MCC, excluyendo cuentas administradoras
google-ads-mcp-server connect --client claude-desktop --manager 9999999999 --all-mcc-clients --preset agency
```

Iniciá sesión en el navegador con la cuenta de Google que tiene acceso a Google Ads. El CLI comprueba las cuentas seleccionadas, guarda el perfil local y registra el MCP en modo de solo lectura. Si omitís `--customer` y hay varias cuentas accesibles, el CLI te pide los IDs.

Podés reutilizar el perfil en otro cliente sin autorizar de nuevo:

```sh
google-ads-mcp-server register --client codex
google-ads-mcp-server register --client claude-code
```

### Elegir qué tools se cargan (`--preset` / `--modules`)

| Preset | Módulos activos | Herramientas | Para qué |
| --- | --- | ---: | --- |
| `core` *(default)* | `core` | 25 | Visión general, GAQL, trends, reportes, waste, alertas |
| `leads` | `core`, `diagnostics`, `measurement`, `search` | 54 | Auditoría de lead-gen (Search + conversiones) |
| `ecommerce` | `core`, `diagnostics`, `measurement`, `pmax`, `shopping` | 47 | PMax + Shopping + tracking |
| `agency` | `core`, `diagnostics`, `measurement`, `search`, `pmax`, `mcc` | 71 | Operación multi-cuenta MCC |
| `full` | Todos los módulos de lectura (+ `operations` si `READ_ONLY_MODE=false`) | 74 *(142 con writes)* | Lectura completa o surface read+write |

`connect` siempre arranca con `READ_ONLY_MODE=true` y las 68 herramientas de `operations` **quedan ocultas**, incluso con `--preset full`.

### Clientes soportados por `--client`

| Cliente | Valor | Resultado |
| --- | --- | --- |
| Claude Desktop | `claude-desktop` | Agrega el server a `claude_desktop_config.json` |
| Claude Code | `claude-code` | Usa `claude mcp add-json --scope user` |
| Codex (CLI / Desktop / IDE) | `codex` | Agrega a `~/.codex/config.toml` |
| Antigravity | `antigravity` | Agrega a `~/.gemini/config/mcp_config.json` |
| Gemini CLI | `gemini-cli` | Agrega a `~/.gemini/settings.json` |
| Cursor / VS Code / Cherry Studio / Cline / Kimi / otros | `generic` | Imprime el JSON `{"mcpServers": ...}` por stdout |

### Antigravity con la versión 0.2.2

La versión 0.2.2 escribe en la ruta antigua por defecto. En PowerShell, indicá la carpeta global actual antes de ejecutar `connect` o `register`:

```powershell
$env:ANTIGRAVITY_CONFIG_DIR = Join-Path $env:USERPROFILE '.gemini\config'
google-ads-mcp-server register --client antigravity
```

Si todavía no tenés un perfil, usá `google-ads-mcp-server connect --client antigravity` en lugar de `register`. En macOS o Linux, configurá la carpeta con `export ANTIGRAVITY_CONFIG_DIR="$HOME/.gemini/config"`.

## 4. Verificar la conexión

```sh
google-ads-mcp-server doctor
google-ads-mcp-server doctor --offline
google-ads-mcp-server doctor --json
```

`doctor` verifica el perfil y el acceso a las cuentas. `--offline` comprueba la configuración local sin conectarse a Google. Reiniciá el cliente MCP y pedile que ejecute `server_status`, liste tus cuentas con `list_accounts` y consulte el rendimiento de una campaña autorizada.

## Problemas frecuentes

| Síntoma | Qué revisar |
| --- | --- |
| Google bloquea el consentimiento o devuelve `access_denied` | Revisá **Audience**, el correo autorizado y la configuración del permiso `adwords`. Si el proyecto está en **Testing**, confirmá que el correo figura en **Test users**. |
| `invalid_grant` | Si la autorización venció o fue revocada, ejecutá `connect` de nuevo. |
| `CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` | Solicitá **Explorer** o superior para el proyecto dueño del cliente OAuth. |
| `USER_PERMISSION_DENIED` | Confirmá que el correo tiene acceso a la cuenta y, si corresponde, pasá el ID del administrador con `--manager`. |
| `OAuth client was not found` | Verificá que el cliente OAuth siga activo y que el JSON corresponda a una **Desktop app**. |
| `CUSTOMER_NOT_ENABLED` o cuenta no encontrada | Revisá el ID de diez dígitos y el estado de la cuenta en Google Ads. |
| `doctor` indica que la consulta no devolvió filas | Probá un período con actividad. Los informes de rendimiento suelen usar días completos en la zona horaria de la cuenta. |
| `google_ads_mcp is already configured` | Si querés cambiar de modo o de perfil, eliminá la entrada anterior del cliente y volvé a registrar. |

## Dónde se guarda cada cosa

| Sistema | Perfil local predeterminado |
| --- | --- |
| Windows | `%LOCALAPPDATA%\google-ads-mcp\profile.json` |
| macOS | `~/Library/Application Support/google-ads-mcp/profile.json` |
| Linux | `${XDG_CONFIG_HOME:-$HOME/.config}/google-ads-mcp/profile.json` |

El perfil contiene el refresh token y los IDs de las cuentas. El archivo local se guarda como JSON sin cifrar. Mantené privados el perfil y el JSON descargado. La configuración del cliente MCP referencia la ruta del perfil.
