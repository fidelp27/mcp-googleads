# Piloto remoto de Google Ads MCP

El gateway presenta una URL MCP común, valida tokens OAuth y asigna cada identidad a un worker privado. Cada worker usa un perfil Google Ads distinto y conserva su configuración, aprobaciones y auditoría en directorios propios. Con `--allow-writes` carga el catálogo completo, incluidas las operaciones de escritura con los controles de aprobación y límites del servidor.

## Preparar el entorno

1. Instalá `python -m pip install ".[remote]"`. Ejecutá el gateway y los workers en el mismo host dedicado, con perfiles y directorios privados.
2. Configurá un emisor OAuth compatible con los clientes del piloto. El gateway espera un JWT firmado con RS256, un `sub` estable, audiencia igual a la URL pública de `/mcp` y el scope `mcp:access`. Configurá el descubrimiento, el registro de clientes y los redirects en ese emisor.
3. Prepará un perfil por usuario mediante el flujo existente de Google Ads `connect`. Cada perfil necesita las cuentas expresamente autorizadas por su titular. Para probar cambios, usá cuentas de prueba y permisos de Google Ads adecuados. Si dos usuarios operan la misma cuenta, sus estados de aprobación son independientes y los bloqueos locales de un worker no coordinan con el otro.
4. Compartí `MCP_REMOTE_WORKER_KEY` entre gateway y workers como secreto de despliegue. Generá un valor aleatorio de al menos 32 caracteres ASCII alfanuméricos, por ejemplo con `python -c "import secrets; print(secrets.token_hex(32))"`. Restringí el acceso remoto a los puertos de los workers.

## Arrancar las instancias

En procesos separados, con `OPERATOR_NAME` configurado para la identidad de cada worker (respetando `--preset` / `--modules`; solo usa `full` por defecto cuando se pasa `--allow-writes` sin preset):

```console
google-ads-mcp-server --profile /srv/profiles/user-a.json serve --worker-port 8101 --preset ecommerce
google-ads-mcp-server --profile /srv/profiles/user-b.json serve --worker-port 8102 --preset agency --allow-writes
```

Configurá el gateway con estos valores de ejemplo:

| Variable | Valor |
| --- | --- |
| `MCP_REMOTE_RESOURCE_URL` | `https://ads-mcp.example.com/mcp` |
| `MCP_REMOTE_ISSUER` | `https://login.example.com` |
| `MCP_REMOTE_JWKS_URL` | URL HTTPS de claves públicas del emisor |
| `MCP_REMOTE_WORKERS` | `{"subject-a":"http://127.0.0.1:8101/mcp","subject-b":"http://127.0.0.1:8102/mcp"}` |
| `MCP_REMOTE_ALLOWED_ORIGINS` | Lista opcional de Origins confiables separados por espacios |
| `PORT` | Puerto local del gateway, `8080` por defecto |

Ejecutá `google-ads-mcp-server remote`. El proxy HTTPS debe reenviar `/mcp` y `/.well-known/oauth-protected-resource/mcp` al gateway, permitir streaming y validar el nombre de host público. El gateway valida la identidad y el recurso OAuth y reenvía la petición solo al worker asignado.

## Verificar antes de ampliar el piloto

- En Codex CLI y Claude Desktop, configurá la misma URL. Entrá como los dos usuarios y verificá listado de herramientas, informes y rechazo cuando se consulta una cuenta fuera del perfil propio.
- Con una cuenta de prueba, ejecutá un `dry_run` con una herramienta de escritura. Verificá que la aprobación emitida por un usuario no se pueda usar desde el worker del otro. Completá una mutación de prueba y confirmá su resultado y auditoría.
- Los argumentos de archivo en remoto apuntan al directorio `<perfil>-files` del worker. Para `analyze_auction_insights_csv`, también podés pegar el texto del CSV directamente en `csv_content` (hasta 2 MB) sin necesidad de subir archivos al host.

---

## Modo B · Beta autogestionada (Self-Serve para cualquier usuario)

Para permitir que cualquier usuario autenticado conecte su propia cuenta de Google Ads desde el navegador —sin aprovisionar perfiles ni puertos a mano— activá el modo self-serve configurando `MCP_REMOTE_STATE_DIR`, una clave maestra Fernet y un cliente OAuth **Web application** de Google Cloud.

### Variables adicionales para modo self-serve

| Variable | Requerida | Descripción |
| --- | :---: | --- |
| `MCP_REMOTE_STATE_DIR` | Sí | Directorio privado donde se almacena `tenants.db` (SQLite) y los directorios aislados de cada worker. |
| `MCP_REMOTE_MASTER_KEY` | Sí | Clave Fernet de 32 bytes (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). Cifra todos los `refresh_token` en SQLite y los perfiles efímeros por instancia (`profile-<id>.json`) de cada worker (`format: "fernet-v1"`). |
| `MCP_REMOTE_GOOGLE_CLIENT_ID` y `MCP_REMOTE_GOOGLE_CLIENT_SECRET` *(o `MCP_REMOTE_GOOGLE_CLIENT_JSON`)* | Sí | Credenciales OAuth de Google Cloud con redirect autorizado `https://<tu-dominio>/connect/google/callback`. |
| `MCP_REMOTE_PORTAL_CLIENT_ID` *(y opcional `MCP_REMOTE_PORTAL_CLIENT_SECRET`)* | Recomendada | Habilita el botón de inicio de sesión web OIDC/OAuth + PKCE en `/connect/login` (callback `https://<tu-dominio>/connect/oidc/callback`) contra el mismo `MCP_REMOTE_ISSUER`. Sin ella, el enlace `/connect/ticket?t=...` que devuelve `/mcp` igual inicia sesión en 1 clic sin pedir Bearer tokens. |
| `MCP_REMOTE_ALLOW_USER_WRITES` | No (`false`) | Por defecto **solo lectura** para todos los usuarios. Si es `true`, el portal sugiere el preset `full` y agrega automáticamente el módulo `operations` si el usuario habilita escrituras con otro preset. La política vigente se aplica siempre en tiempo de arranque del worker (desactivarla pasa a solo lectura incluso a perfiles guardados previamente con escrituras). |
| `MCP_REMOTE_IDLE_TIMEOUT_SECONDS` | No (`900`) | Tiempo de inactividad (15 min por defecto) tras el cual el pool apaga automáticamente workers sin solicitudes en curso (`in_flight == 0`). |
| `MCP_REMOTE_MAX_WORKERS` | No (`50`) | Tope estricto de workers simultáneos (plaza reservada atómicamente antes de arrancar cada proceso; devuelve `HTTP 503 Retry-After: 2` si todos los workers activos tienen solicitudes en curso). |

Con estas variables configuradas (sin necesidad de definir `MCP_REMOTE_WORKERS`), arrancá únicamente:

```console
google-ads-mcp-server remote
```

### Rutas del portal self-serve (en el mismo proxy HTTPS)

El proxy HTTPS debe reenviar `/mcp`, `/.well-known/oauth-protected-resource/mcp` y `/connect*` al gateway:

- `GET /connect/ticket?t=<ticket>`: canjea en 1 clic el ticket de un solo uso emitido por `/mcp` cuando el usuario aún no conectó Google Ads, fija la cookie `HttpOnly; Secure; SameSite=Lax` y redirige a `/connect` con `Cache-Control: no-store` y `Referrer-Policy: no-referrer`. El gateway desactiva `access_log` por defecto; configurá también tu proxy HTTPS (Caddy/Nginx) para **no registrar query strings** en `/connect/ticket`, `/connect/oidc/callback` y `/connect/google/callback`.
- `GET /connect/login` y `GET /connect/oidc/callback`: inicio de sesión web OIDC/OAuth + PKCE contra `MCP_REMOTE_ISSUER`, ligado al navegador iniciador mediante cookie `HttpOnly` cifrada `mcp_oidc_tx` (prevención login-CSRF según RFC 9700) y verificado con el mismo `OAuthVerifier` para vincular el mismo `sub`.
- `POST /connect/session`: alternativa API para intercambiar un Bearer JWT válido por cookie de sesión.
- `GET /connect` y `GET /connect/status`: página HTML o estado JSON de la conexión (mostrando cuentas, rutas por MCC, preset y herramientas activas).
- `GET /connect/google/start`: inicia OAuth 2.0 + PKCE (`S256`) + `state` CSRF ligado al `sub` del usuario.
- `GET /connect/google/callback`: intercambia el código con Google, descubre cuentas directas y subcuentas activas por cada MCC y emite un `grant_token` de 10 minutos con `authorized_routes` ligado al `disconnect_epoch` actual (si se inició o completó una desconexión mientras el intercambio estaba en vuelo, rechaza con `HTTP 409` sin emitir `grant_token`).
- `POST /connect/configure`: valida cada par `(customer_id, manager_id)` contra el grant (guardando `customer_routes` por cuenta para soportar mezclas de cuentas directas y múltiples MCC sin fuga de headers, y bloqueando cambios de ruta/MCC sin reautorizar), alinea `allow_writes` con el módulo `operations` y drena/reinicia el worker del usuario.
- `POST /connect/disconnect`: bloquea inmediatamente nuevas solicitudes `/mcp` y borra las credenciales cifradas en SQLite. Si el worker no tiene solicitudes en curso y Google responde la revocación, detiene el worker, borra su perfil efímero y responde `HTTP 200` (`status: "disconnected"`). Si el worker sigue drenando una solicitud en vuelo **o** la revocación en Google aún está en curso, responde `HTTP 202` (`status: "disconnecting"`, `revocation_pending: true`) de forma idempotente ante reintentos (y `/connect/status` mantiene `disconnecting: true`), difiriendo el cierre y bloqueando cualquier reconexión (`/connect/google/start`, `/connect/google/callback`, `/connect/configure`) con `HTTP 409 Conflict` (`disconnect_in_progress`, `Retry-After: 2`) hasta que concluyan tanto el drenaje como la respuesta de revocación de Google. Así una revocación anterior nunca puede invalidar credenciales recién emitidas del mismo proyecto OAuth.

Si un usuario autenticado llama a `/mcp` antes de vincular Google Ads, el gateway responde `HTTP 403` con `{"error": "google_ads_not_connected", "connect_url": "https://<tu-dominio>/connect/ticket?t=<ticket>"}`.

### Requisitos de Google Cloud antes de abrirlo a público general

1. **Verificación OAuth de Google (Google OAuth App Verification):** el scope `https://www.googleapis.com/auth/adwords` es sensible; sin verificación, Google limita la app a 100 usuarios de prueba agregados manualmente. Prepará dominio propio, política de privacidad, términos y video demo del flujo `/connect`.
2. **Acceso de producción en Google Ads API:** el proyecto de Cloud / Developer Token debe contar con acceso Basic o Standard para consultar cuentas reales de terceros.
3. **Escala futura (100+ usuarios concurrentes):** este modo con pool dinámico cubre cómodamente decenas de usuarios simultáneos en un VPS sin reescribir tools. Al superar ~50–100 usuarios concurrentes, el siguiente paso es migrar a ejecución multi-tenant en un solo proceso sobre `agency/runtime.py` (`RuntimeContext` + `StorageBackend` en Postgres + KMS).
