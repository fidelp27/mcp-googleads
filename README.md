# Google Ads MCP

Servidor MCP independiente para consultar, auditar y analizar cuentas de Google Ads desde Claude, ChatGPT, Codex y cualquier cliente compatible con Model Context Protocol.

El proyecto ofrece herramientas listas para usar y acceso flexible a Google Ads Query Language (GAQL). Puede ejecutarse localmente por `stdio` o publicarse mediante Streamable HTTP.

## Funcionalidades

- Descubrimiento de cuentas de Google Ads accesibles.
- Estado de configuración sin exponer tokens, secretos ni rutas sensibles.
- Rendimiento de campañas y anuncios por rango de fechas.
- Consulta de moneda de la cuenta para interpretar costos correctamente.
- Ejecución de consultas GAQL personalizadas.
- Resultados GAQL en tabla, JSON o CSV.
- Auditoría de titulares, descripciones y URLs de anuncios.
- Inventario y descarga local de imágenes publicitarias.
- Identificación del uso de assets en campañas y grupos de anuncios.
- Análisis de impresiones, clics y conversiones por imagen.
- Exploración de recursos válidos mediante `GoogleAdsFieldService`.
- OAuth con persistencia y renovación de tokens.
- Autenticación no interactiva mediante cuenta de servicio.
- Transportes MCP `stdio` y Streamable HTTP.
- Imagen Docker y scripts de instalación, autorización y prueba real.
- Timeouts configurables y validación de IDs, límites y rangos.

El servidor actualmente consulta datos y no modifica campañas, presupuestos ni anuncios. La única herramienta que escribe es `download_image_asset`, y lo hace exclusivamente en el disco local.

## Compatibilidad

| Cliente | Transporte | Uso |
|---|---|---|
| Claude Desktop | `stdio` | Servidor local |
| ChatGPT Desktop con Codex | `stdio` | Servidor local |
| Codex CLI / IDE | `stdio` | Servidor local |
| ChatGPT web u otro cliente remoto | Streamable HTTP | Servidor desplegado con HTTPS |

ChatGPT Desktop con Codex, Codex CLI y la extensión de IDE comparten la configuración de `~/.codex/config.toml`. ChatGPT web no puede acceder directamente a `localhost`: necesita un endpoint remoto HTTPS o un túnel seguro.

## Requisitos

- Python 3.11 o superior.
- Una cuenta de Google Ads, preferiblemente vinculada a una cuenta administradora.
- Un proyecto de Google Cloud con Google Ads API habilitada.
- Un Developer Token de Google Ads.
- Credenciales OAuth de usuario o una cuenta de servicio.
- Claude, ChatGPT, Codex u otro cliente MCP.

El servidor usa Google Ads API `v25` por defecto. Las versiones retiradas dejan de funcionar; revisa el [calendario oficial de versiones](https://developers.google.com/google-ads/api/docs/sunset-dates) antes de cambiar `GOOGLE_ADS_API_VERSION`.

## Instalación rápida

### Windows

```powershell
.\scripts\setup.ps1
Copy-Item .env.example .env
```

### macOS o Linux

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
```

La instalación de Windows también genera ejemplos listos para copiar en `generated/`.

## Cómo obtener las credenciales

Google Ads API requiere dos mecanismos diferentes:

1. **Developer Token:** identifica el software que llama a la API.
2. **OAuth 2.0:** identifica al usuario o cuenta de servicio que tiene acceso a las cuentas publicitarias.

Tener uno no reemplaza al otro. También necesitarás el ID de la cuenta administradora cuando consultes cuentas cliente mediante un MCC.

### 1. Crear o elegir una cuenta administradora

El Developer Token solo se obtiene desde una [cuenta administradora de Google Ads](https://ads.google.com/home/tools/manager-accounts/).

1. Entra en tu cuenta administradora.
2. Vincula las cuentas publicitarias que quieras consultar.
3. Conserva su ID de diez dígitos; será el valor de `GOOGLE_ADS_LOGIN_CUSTOMER_ID`.

El ID puede escribirse con o sin guiones. No coloques aquí el ID de la cuenta cliente que consultarás, sino el de la cuenta administradora usada para acceder a ella.

### 2. Solicitar el Developer Token

1. Inicia sesión en la cuenta administradora.
2. Abre el [API Center de Google Ads](https://ads.google.com/aw/apicenter).
3. Completa el formulario de acceso y acepta los términos.
4. Copia el Developer Token y guárdalo como `GOOGLE_ADS_DEVELOPER_TOKEN`.

Google puede asignar acceso de prueba o Explorer inicialmente. El nivel otorgado determina si puedes consultar cuentas de producción y cuántas operaciones puedes realizar. Consulta la documentación oficial sobre el [Developer Token](https://developers.google.com/google-ads/api/docs/api-policy/developer-token) y sus [niveles de acceso](https://developers.google.com/google-ads/api/docs/api-policy/access-levels).

Trata este token como una contraseña. Si se filtra, restablécelo desde el API Center.

### 3. Crear un proyecto de Google Cloud

1. Abre la [consola de Google Cloud](https://console.cloud.google.com/).
2. Crea un proyecto o selecciona uno dedicado a esta integración.
3. Habilita la [Google Ads API](https://console.cloud.google.com/apis/library/googleads.googleapis.com).
4. Elige uno de los métodos siguientes.

No reutilices sin revisar un proyecto que ya fue usado con el Developer Token de otra cuenta administradora. Google vincula el proyecto y el token, y una combinación incompatible puede producir `DEVELOPER_TOKEN_PROHIBITED`.

### Opción A: OAuth de usuario

Es la opción más directa para desarrollo local o una instalación usada por una sola persona.

1. En Google Cloud abre **Google Auth Platform**.
2. Configura la pantalla de consentimiento.
3. Agrega el scope `https://www.googleapis.com/auth/adwords`.
4. Si la aplicación está en modo de prueba, agrega como usuario de prueba la cuenta de Google que tiene acceso a Google Ads.
5. Crea un cliente OAuth del tipo **Desktop app**.
6. Descarga el JSON y guárdalo fuera del repositorio, por ejemplo:

```text
C:\Users\TU_USUARIO\.secrets\google-ads\client_secret.json
```

7. Configura `.env`:

```dotenv
GOOGLE_ADS_AUTH_TYPE=oauth
GOOGLE_ADS_CREDENTIALS_PATH=C:/Users/TU_USUARIO/.secrets/google-ads/client_secret.json
GOOGLE_ADS_TOKEN_PATH=C:/Users/TU_USUARIO/.secrets/google-ads/google_ads_token.json
GOOGLE_ADS_DEVELOPER_TOKEN=tu_developer_token
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890
```

8. Autoriza el acceso:

```powershell
.\.venv\Scripts\python.exe .\scripts\authorize_oauth.py
```

Se abrirá el navegador. Inicia sesión con un usuario que tenga acceso a las cuentas de Google Ads y acepta el permiso solicitado. El proyecto guardará un refresh token en `GOOGLE_ADS_TOKEN_PATH` y lo renovará automáticamente.

No uses la misma ruta para `GOOGLE_ADS_CREDENTIALS_PATH` y `GOOGLE_ADS_TOKEN_PATH`: el primer archivo contiene el cliente OAuth y el segundo contiene la autorización del usuario.

También puedes proporcionar `GOOGLE_ADS_CLIENT_ID` y `GOOGLE_ADS_CLIENT_SECRET` sin un archivo de cliente, pero el JSON descargado reduce errores de configuración. Consulta el flujo oficial de [autenticación para un solo usuario](https://developers.google.com/google-ads/api/docs/oauth/single-user-authentication).

### Opción B: cuenta de servicio

Es la opción recomendada para automatizaciones, servidores y procesos que no deben depender de una sesión humana.

1. En Google Cloud abre **IAM y administración → Cuentas de servicio**.
2. Crea una cuenta de servicio dedicada.
3. Crea y descarga una clave JSON.
4. En Google Ads abre **Administración → Acceso y seguridad**.
5. Agrega el correo de la cuenta de servicio como usuario con el nivel de acceso mínimo necesario.
6. Si administras muchas cuentas, agrega la cuenta de servicio al MCC en lugar de hacerlo cuenta por cuenta.
7. Configura `.env`:

```dotenv
GOOGLE_ADS_AUTH_TYPE=service_account
GOOGLE_ADS_CREDENTIALS_PATH=/ruta/segura/google-ads-service-account.json
GOOGLE_ADS_DEVELOPER_TOKEN=tu_developer_token
GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890
GOOGLE_ADS_IMPERSONATION_EMAIL=
```

En el flujo habitual no necesitas `GOOGLE_ADS_IMPERSONATION_EMAIL`: Google Ads puede conceder acceso directamente al correo de la cuenta de servicio. La impersonación solo aplica si tu organización configuró delegación de todo el dominio.

No subas la clave JSON al repositorio ni la incluyas dentro de una imagen Docker. Consulta el flujo oficial para [cuentas de servicio](https://developers.google.com/google-ads/api/docs/oauth/service-accounts).

### 4. Comprobar la configuración

Primero inicia el servidor y ejecuta la herramienta:

```text
server_status
```

Debe devolver `"ready": true`. Luego ejecuta la prueba contra la API real:

```powershell
.\.venv\Scripts\python.exe .\scripts\smoke_google_ads.py
```

La prueba lista cuentas accesibles, obtiene la moneda y consulta siete días de rendimiento. No modifica datos.

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---:|---|
| `GOOGLE_ADS_AUTH_TYPE` | Sí | `oauth` o `service_account`. |
| `GOOGLE_ADS_CREDENTIALS_PATH` | Sí | JSON del cliente OAuth, token autorizado o clave de cuenta de servicio. |
| `GOOGLE_ADS_TOKEN_PATH` | OAuth | Archivo separado donde se guarda el token autorizado. |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | Sí | Token obtenido en el API Center de la cuenta administradora. |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | Según acceso | ID del MCC usado para acceder a cuentas cliente. |
| `GOOGLE_ADS_CLIENT_ID` | Alternativa OAuth | Cliente OAuth si no se usa un JSON de cliente. |
| `GOOGLE_ADS_CLIENT_SECRET` | Alternativa OAuth | Secreto OAuth si no se usa un JSON de cliente. |
| `GOOGLE_ADS_IMPERSONATION_EMAIL` | No | Usuario delegado por una cuenta de servicio. |
| `GOOGLE_ADS_HTTP_TIMEOUT_SECONDS` | No | Timeout por solicitud; valor predeterminado: `30`. |
| `GOOGLE_ADS_API_VERSION` | No | Versión REST; valor predeterminado: `v25`. |
| `MCP_TRANSPORT` | No | `stdio` o `streamable-http`. |
| `MCP_HOST` | No | Dirección HTTP; valor predeterminado: `127.0.0.1`. |
| `MCP_PORT` | No | Puerto HTTP; valor predeterminado: `8000`. |

## Herramientas MCP

| Herramienta | Qué hace | Escritura |
|---|---|---|
| `server_status` | Comprueba credenciales, tipo de autenticación y versión sin revelar secretos. | No |
| `list_accounts` | Lista los IDs de Google Ads accesibles por las credenciales. | No |
| `get_account_currency` | Devuelve la moneda predeterminada de una cuenta. | No |
| `get_campaign_performance` | Resume impresiones, clics, conversiones y costos por campaña. | No |
| `get_ad_performance` | Resume rendimiento a nivel de anuncio. | No |
| `get_ad_creatives` | Recupera titulares, descripciones, estados y URLs. | No |
| `execute_gaql_query` | Ejecuta una consulta GAQL y devuelve resultados formateados. | No |
| `run_gaql` | Ejecuta GAQL con salida `table`, `json` o `csv`. | No |
| `get_image_assets` | Lista imágenes y sus URLs en tamaño completo. | No |
| `download_image_asset` | Descarga una imagen en un directorio local. | Solo disco local |
| `get_asset_usage` | Muestra dónde se utiliza un asset por ID y tipo. | No |
| `analyze_image_assets` | Cruza imágenes con impresiones, clics y conversiones. | No |
| `list_resources` | Lista recursos válidos para cláusulas `FROM` de GAQL. | No |

Los rangos `days` aceptan entre 1 y 365 días. Los costos de Google Ads se reciben en micros: `1.000.000` micros equivale a una unidad de la moneda de la cuenta.

### Flujo recomendado

1. Ejecuta `server_status`.
2. Ejecuta `list_accounts`.
3. Usa `get_account_currency` con la cuenta elegida.
4. Consulta rendimiento, creatividades o assets.
5. Usa `run_gaql` cuando necesites dimensiones o métricas no cubiertas por las herramientas preparadas.

Ejemplos de pedidos para el asistente:

```text
Lista mis cuentas accesibles y dime la moneda de cada una.
```

```text
Analiza el rendimiento de campañas de los últimos 30 días y señala gasto sin conversiones.
```

```text
Compara las imágenes publicitarias por CTR y conversiones de los últimos 14 días.
```

Para crear consultas propias, revisa la [documentación oficial de GAQL](https://developers.google.com/google-ads/api/docs/query/overview) y los ejemplos incluidos en `docs/`.

## Configurar clientes MCP

### Claude Desktop

En Windows, `scripts/setup.ps1` genera `generated/claude_desktop_config.snippet.json`. Copia la entrada `googleAdsMcp` dentro de `mcpServers` en:

```text
%APPDATA%\Claude\claude_desktop_config.json
```

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

Reinicia Claude Desktop después del cambio.

### ChatGPT Desktop y Codex

Agrega esta entrada a `C:\Users\TU_USUARIO\.codex\config.toml`:

```toml
[mcp_servers.google_ads_mcp]
enabled = true
command = 'C:\RUTA\AL\PROYECTO\.venv\Scripts\google-ads-mcp.exe'
args = ["--transport", "stdio"]
cwd = 'C:\RUTA\AL\PROYECTO'
startup_timeout_sec = 30
tool_timeout_sec = 120
```

Reinicia el cliente para que vuelva a descubrir las herramientas.

### Ejecución manual

Servidor local por `stdio`:

```bash
google-ads-mcp --transport stdio
```

Servidor HTTP:

```bash
google-ads-mcp --transport streamable-http --host 0.0.0.0 --port 8000
```

El endpoint local queda en `http://127.0.0.1:8000/mcp`.

## Despliegue remoto

Construye la imagen:

```bash
docker build -t google-ads-mcp .
```

Ejemplo de ejecución:

```bash
docker run --rm -p 8000:8000 --env-file .env \
  -v /ruta/segura:/run/secrets:ro \
  google-ads-mcp
```

Las rutas configuradas en `.env` deben apuntar al volumen del contenedor, por ejemplo `/run/secrets/google-ads-service-account.json`.

Para conectar ChatGPT web:

1. Publica `/mcp` detrás de HTTPS.
2. Protege el endpoint con autenticación.
3. Habilita Developer Mode en ChatGPT.
4. Crea una app MCP con `https://tu-dominio/mcp`.
5. Escanea las herramientas y prueba `server_status`.

No publiques este servidor sin autenticación. Es single-tenant y todas las solicitudes usan las credenciales configuradas en el proceso. Un SaaS multiusuario necesita OAuth por usuario, autorización propia y almacenamiento aislado de tokens.

## Validación y desarrollo

Pruebas automatizadas:

```bash
python -m pytest
```

Las pruebas cubren:

- validación de IDs, límites y fechas;
- diagnóstico sin exposición de secretos;
- persistencia segura del cliente y token OAuth;
- descubrimiento de herramientas por `stdio`;
- descubrimiento y llamadas por Streamable HTTP;
- acceso a metadatos mediante `GoogleAdsFieldService`.

Prueba real con Google Ads:

```bash
python scripts/authorize_oauth.py
python scripts/smoke_google_ads.py
```

`authorize_oauth.py` solo es necesario para OAuth. `smoke_google_ads.py` realiza lecturas reales y requiere credenciales válidas.

## Problemas frecuentes

### `server_status` devuelve `ready: false`

Lee el campo `issues`. Normalmente falta el Developer Token, la ruta de credenciales no existe o todavía no se generó el token OAuth.

### `DEVELOPER_TOKEN_PROHIBITED`

El proyecto de Google Cloud y el Developer Token pertenecen a combinaciones incompatibles. Crea un proyecto dedicado o usa el token originalmente asociado al proyecto.

### `USER_PERMISSION_DENIED`

El usuario OAuth o la cuenta de servicio no tiene acceso a la cuenta solicitada. Si accedes mediante un MCC, configura también `GOOGLE_ADS_LOGIN_CUSTOMER_ID`.

### No aparecen todas las cuentas

`list_accounts` devuelve las cuentas accesibles directamente por la identidad autenticada. Verifica los vínculos del MCC y los permisos del usuario o cuenta de servicio.

### OAuth abre el navegador en cada inicio

Comprueba que `GOOGLE_ADS_TOKEN_PATH` sea escribible, persista entre ejecuciones y no apunte al mismo archivo que `GOOGLE_ADS_CREDENTIALS_PATH`.

## Seguridad y operación

- `.env`, tokens, claves y credenciales están ignorados por Git.
- Los secretos deben vivir en un gestor de secretos o volumen de solo lectura en producción.
- `server_status` no devuelve valores ni rutas sensibles.
- Las llamadas HTTP tienen timeout configurable.
- Los IDs y límites interpolados en GAQL se validan.
- Las herramientas MCP están marcadas como lectura o escritura local.
- Rota inmediatamente cualquier Developer Token, cliente OAuth o clave JSON expuesta.

## Licencia

MIT. Consulta [LICENSE](LICENSE). El aviso anterior se conserva porque la licencia MIT exige mantenerlo en copias o porciones sustanciales; Fidel es titular de las modificaciones realizadas desde 2026.
