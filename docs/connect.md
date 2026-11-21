# Conectar Google Ads a un cliente MCP de escritorio

## Probar en 30 segundos sin credenciales (`--demo`)

Antes de configurar Google Cloud, podés probar el servidor en modo solo lectura con 2 cuentas sintéticas (un ecommerce en `EUR` y una cuenta B2B Leads en `USD`):

```console
python -m pip install .
google-ads-mcp-server register --client claude-desktop --demo
```

También podés iniciar directamente por `stdio`:

```console
google-ads-mcp-server --transport stdio --demo
```

## Requisitos para cuentas reales

- Python 3.11 o superior y un cliente MCP con soporte `stdio`.
- Un proyecto de Google Cloud con Google Ads API activada, un cliente OAuth de tipo **Desktop app** y acceso para la cuenta de destino. Para cuentas de producción, revisá el [nivel de acceso del proyecto](https://developers.google.com/google-ads/api/docs/api-policy/access-levels). El flujo nuevo usa el proyecto de Cloud sin solicitar Developer Token ([cambios de acceso](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)).
- Descargá el JSON del cliente OAuth. Si la app está en modo de prueba, agregá como usuario de prueba la cuenta de Google que hará la conexión en la pantalla de consentimiento.

## Instalar, autorizar y verificar (`connect` + `doctor`)

```console
python -m pip install .
google-ads-mcp-server connect --client codex --oauth-client /ruta/client_secret.json
google-ads-mcp-server doctor
```

En Windows, usá la ruta entre comillas si contiene espacios. Si omitís `--oauth-client`, el asistente te mostrará los pasos de Google Cloud y te pedirá la ruta. Con `--no-browser` imprime la URL para abrirla manualmente.

| Cliente | Valor para `--client` | Resultado |
| --- | --- | --- |
| Claude Desktop (Windows / macOS) | `claude-desktop` | Agrega el servidor a `claude_desktop_config.json`. |
| Claude Code | `claude-code` | Usa `claude mcp add-json --scope user`. |
| Codex (CLI / Desktop / IDE) | `codex` | Agrega el servidor a `~/.codex/config.toml`. |
| Antigravity | `antigravity` | Agrega el servidor a `~/.gemini/antigravity/mcp_config.json`. |
| Gemini CLI | `gemini-cli` | Agrega el servidor a `~/.gemini/settings.json`. |
| Cualquier otro cliente (Cursor, VS Code, Cherry Studio, Cline, Kimi, etc.) | `generic` (o `cli`) | Imprime el bloque estándar `{"mcpServers": ...}` en JSON para pegar en tu cliente. |

### Cuenta directa, múltiples cuentas y MCC

- **Cuenta directa:** `--customer 1234567890`
- **Varias cuentas explícitas:** `--customer 1111111111,2222222222`
- **Cuenta(s) bajo MCC:** `--manager 9999999999 --customer 1234567890`
- **Todas las cuentas activas bajo un MCC:** `--manager 9999999999 --all-mcc-clients`

El perfil siempre arranca en **solo lectura (`READ_ONLY_MODE=true`)** y limita las consultas a las cuentas seleccionadas y, cuando corresponde, a su administrador.

### Elegir catálogo de tools (`--preset` / `--modules`)

Por defecto el perfil registra el módulo compacto `core` (25 tools). Para habilitar módulos adicionales de solo lectura (`diagnostics`, `measurement`, `search`, `pmax`, `shopping`, `display`, `demand-gen`, `mcc`) en clientes registrados:

```console
google-ads-mcp-server connect --client claude-desktop --preset ecommerce
google-ads-mcp-server register --client codex --preset agency
google-ads-mcp-server register --client generic --demo --preset leads
google-ads-mcp-server register --client antigravity --modules diagnostics,measurement,search
```

Incluso con `--preset full`, el perfil gestionado mantiene `READ_ONLY_MODE=true` y nunca expone tools de escritura (`operations`).

### Reutilizar perfil y diagnóstico (`register` / `doctor`)

- Para registrar el mismo perfil en otro cliente: `google-ads-mcp-server register --client antigravity`.
- Para usar un perfil adicional: `--profile /ruta/personal/profile.json` antes del subcomando (siempre fuera de Git).
- Para diagnosticar credenciales, acceso a Google Ads, nivel de proyecto Cloud o ausencia de datos: `google-ads-mcp-server doctor` (o `doctor --offline` / `doctor --json`).

En una sesión nueva, probá `server_status`, `list_accounts` y `get_campaign_performance`.
