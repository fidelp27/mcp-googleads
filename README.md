# Google Ads MCP

Servidor MCP independiente para consultar y analizar Google Ads desde clientes compatibles. Expone herramientas para cuentas, campañas, anuncios, creatividades, assets y consultas GAQL.

## Compatibilidad

| Cliente | Transporte | Estado |
|---|---|---|
| Claude Desktop | MCP local por `stdio` | Soportado |
| ChatGPT Desktop con host Codex | MCP local por `stdio` | Soportado |
| ChatGPT web | MCP remoto por Streamable HTTP | Soportado |

ChatGPT Desktop con Codex, Codex CLI y la extensión de IDE comparten la configuración MCP local de `~/.codex/config.toml`. ChatGPT web no lee esa configuración y necesita una app con endpoint remoto. OpenAI indica que los servidores locales pueden conectarse a sus productos remotos mediante [Secure MCP Tunnel](https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt).

## Requisitos

- Python 3.11 o superior.
- Acceso a Google Ads API.
- Developer Token de Google Ads.
- OAuth de usuario o una cuenta de servicio con acceso a las cuentas publicitarias.
- Claude Desktop para uso local, o un plan de ChatGPT con acceso a apps MCP en modo desarrollador.

El servidor usa Google Ads API `v25` por defecto. Puede cambiarse con `GOOGLE_ADS_API_VERSION`, pero las versiones retiradas dejan de funcionar. Consulta el [calendario oficial de versiones](https://developers.google.com/google-ads/api/docs/sunset-dates).

## Instalación

En Windows:

```powershell
.\scripts\setup.ps1
Copy-Item .env.example .env
```

En macOS o Linux:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

Completa `.env`:

```dotenv
GOOGLE_ADS_AUTH_TYPE=oauth
GOOGLE_ADS_CREDENTIALS_PATH=C:/ruta/client_secret.json
GOOGLE_ADS_TOKEN_PATH=C:/ruta/google_ads_token.json
GOOGLE_ADS_DEVELOPER_TOKEN=tu_developer_token
GOOGLE_ADS_LOGIN_CUSTOMER_ID=
```

No reutilices el mismo archivo para `GOOGLE_ADS_CREDENTIALS_PATH` y `GOOGLE_ADS_TOKEN_PATH`. El primero contiene la configuración OAuth descargada de Google; el segundo guarda el token generado.

Para un servidor remoto se recomienda una cuenta de servicio o generar el token OAuth localmente antes de desplegar. Un flujo OAuth interactivo no debe ejecutarse dentro de un contenedor sin navegador.

## Claude Desktop

La instalación genera [claude_desktop_config.snippet.json](generated/claude_desktop_config.snippet.json) con rutas absolutas para esta máquina. Copia la entrada `googleAdsMcp` dentro de `mcpServers` en:

- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

Configuración equivalente:

```json
{
  "mcpServers": {
    "googleAdsMcp": {
      "command": "powershell.exe",
      "args": [
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        "C:\\RUTA\\AL\\PROYECTO\\scripts\\run_stdio.ps1"
      ]
    }
  }
}
```

Reinicia Claude Desktop. Ejecuta primero `server_status`; debe devolver `"ready": true`.

## ChatGPT Desktop y Codex

Agrega esta entrada a `C:\Users\TU_USUARIO\.codex\config.toml` en Windows:

```toml
[mcp_servers.google_ads_mcp]
enabled = true
command = 'C:\RUTA\AL\PROYECTO\.venv\Scripts\google-ads-mcp.exe'
args = ["--transport", "stdio"]
cwd = 'C:\RUTA\AL\PROYECTO'
startup_timeout_sec = 30
tool_timeout_sec = 120
```

En ChatGPT Desktop abre `Settings → MCP servers` para verificar la entrada y reinicia la app. También puede comprobarse desde el compositor con `/mcp`.

## ChatGPT web

ChatGPT necesita un servidor MCP remoto con Streamable HTTP. Para probarlo localmente:

```powershell
.\scripts\run_http.ps1
```

El endpoint queda en `http://127.0.0.1:8000/mcp`. Ese endpoint local puede probarse con MCP Inspector, pero ChatGPT no puede accederlo directamente.

Para ChatGPT:

1. Despliega el contenedor detrás de HTTPS o usa Secure MCP Tunnel.
2. En ChatGPT web, habilita Developer Mode.
3. Ve a `Settings → Apps → Create`.
4. Introduce `https://tu-dominio/mcp`.
5. Escanea las herramientas y crea la app.
6. Abre un chat nuevo, selecciona la app y ejecuta `server_status`.

Para un despliegue público no uses el modo sin autenticación. La ruta recomendada es HTTPS más OAuth o Secure MCP Tunnel. Las credenciales de Google Ads deben permanecer en variables de entorno o secretos montados, nunca en la imagen.

### Docker

```bash
docker build -t google-ads-mcp .
docker run --rm -p 8000:8000 --env-file .env \
  -v /ruta/segura:/run/secrets:ro \
  google-ads-mcp
```

Configura las rutas de credenciales de `.env` para que apunten al volumen montado, por ejemplo `/run/secrets/google_ads_token.json`.

## Ejecución manual

Local por `stdio`:

```bash
google-ads-mcp --transport stdio
```

Remoto por HTTP:

```bash
google-ads-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

## Herramientas

- `server_status`
- `list_accounts`
- `execute_gaql_query`
- `get_campaign_performance`
- `get_ad_performance`
- `run_gaql`
- `get_ad_creatives`
- `get_account_currency`
- `get_image_assets`
- `download_image_asset`
- `get_asset_usage`
- `analyze_image_assets`
- `list_resources`

Flujo recomendado:

1. `server_status`
2. `list_accounts`
3. `get_account_currency`
4. Herramienta de análisis o `run_gaql`

## Validación

```bash
python -m pytest
```

Las pruebas verifican validación de entradas, metadatos, descubrimiento MCP por `stdio` y descubrimiento MCP por Streamable HTTP sin llamar a Google Ads.

Las llamadas reales requieren credenciales válidas:

```bash
python scripts/authorize_oauth.py
python scripts/smoke_google_ads.py
```

## Seguridad y operación

- `.env`, tokens y credenciales están ignorados por Git.
- Todas las llamadas HTTP tienen timeout configurable.
- Los IDs y límites interpolados en GAQL se validan.
- `server_status` nunca devuelve secretos ni rutas.
- El servidor remoto es single-tenant: usa las credenciales configuradas en el proceso.
- Para varios clientes o cuentas, la fase siguiente debe incorporar OAuth por usuario y almacenamiento aislado de tokens.

## Licencia

MIT. Consulta [LICENSE](LICENSE). El aviso anterior se conserva porque la licencia MIT exige mantenerlo en copias o porciones sustanciales; Fidel es titular de las modificaciones realizadas desde 2026.
