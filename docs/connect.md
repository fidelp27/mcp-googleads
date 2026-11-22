# Guía de credenciales de Google Ads API

> **¿Solo querés probar sin configurar nada?** Saltá al [modo demo](#atajo-modo-demo-sin-credenciales) y volvés acá cuando quieras conectar una cuenta real.

Esta guía cubre el flujo completo: proyecto en Google Cloud → habilitar la API → configurar OAuth → obtener Developer Token → conectar la cuenta a tu cliente MCP → verificar con `doctor`. Termina en ~15 minutos la primera vez.

---

## Qué vas a necesitar

| Pieza | Para qué sirve | De dónde sale |
| --- | --- | --- |
| **Proyecto en Google Cloud** | Contiene las credenciales OAuth y mide el consumo de API. | [console.cloud.google.com](https://console.cloud.google.com/) |
| **OAuth Client (Desktop app)** JSON | Autoriza al servidor local a actuar en tu nombre. Se descarga como `client_secret.json`. | Google Cloud → APIs & Services → Credentials |
| **Developer Token** | Clave que vincula el proyecto Cloud con Google Ads. No siempre es obligatoria. | Google Ads UI → Tools → API Center |
| **Acceso a la cuenta Google Ads** | Permiso de lectura (como mínimo) sobre la cuenta o MCC que vas a consultar. | Admin de la cuenta Google Ads |
| **Python ≥ 3.11** en tu máquina | Para correr el servidor MCP. | python.org / uv / pipx |

> **Developer Token: cuándo sí y cuándo no.** Google introdujo en 2026 un [nuevo flujo sin Developer Token explícito](https://developers.google.com/google-ads/api/docs/api-policy/developer-token) cuando usás un proyecto Cloud con la API habilitada y acceso directo. Para la mayoría de los casos **vas a necesitar un Developer Token igual** (acceso Basic o Standard para producción, o un token de prueba para empezar). El servidor lee la variable `GOOGLE_ADS_DEVELOPER_TOKEN` si existe.

---

## Atajo: modo demo (sin credenciales)

Si solo querés ver qué puede hacer el MCP, saltate TODO lo anterior:

```bash
uvx google-ads-mcp-server --transport stdio --demo --preset ecommerce
```

O registralo directamente en tu cliente de escritorio:

```bash
google-ads-mcp-server register --client claude-desktop --demo
```

Levanta 2 cuentas sintéticas (`1111111111` EUR ecommerce y `2222222222` USD B2B leads) con datos realistas. **Read-only estricto**, sin tocar Google Cloud ni Google Ads.

---

## Paso 1 — Crear un proyecto en Google Cloud

1. Entrá a [console.cloud.google.com](https://console.cloud.google.com/).
2. Arriba a la izquierda, click en el selector de proyecto → **New Project**.
3. Nombre sugerido: `google-ads-mcp` (o cualquiera). Dejá *No organization* si es personal. **Create**.
4. Esperá unos segundos y seleccioná ese proyecto como activo (el banner arriba debe mostrarlo).

> **Si ya tenés un proyecto de Google Ads API funcionando**, reusalo. No hace falta uno nuevo por cada integración.

---

## Paso 2 — Habilitar la Google Ads API

1. En el menú lateral: **APIs & Services → Library**.
2. Buscá *Google Ads API* y entrá.
3. Click **Enable**. (Si ya está habilitada verás *Manage*.)

---

## Paso 3 — Configurar la pantalla de consentimiento OAuth

1. **APIs & Services → OAuth consent screen**.
2. Elegí **External** (necesario salvo que uses Google Workspace con un tenant específico) → **Create**.
3. Completá los campos mínimos:
   - *App name:* `google-ads-mcp` (o lo que prefieras — nadie más lo va a ver si queda en Testing).
   - *User support email:* tu email.
   - *Developer contact information:* tu email.
4. **Save and continue**.
5. **Scopes:** no añadas nada acá. El servidor pide `https://www.googleapis.com/auth/adwords` en runtime, y así funciona sin publicación pública. **Save and continue**.
6. **Test users:** agregá la cuenta Google con la que vas a autorizar (típicamente, la misma que tiene acceso a Google Ads). **Add users → Save and continue**.
7. **Back to Dashboard**. Dejá la app en estado **Testing**: no necesita verificación de Google para uso personal/interno.

> **Modo Testing vs. Production:**
> - *Testing*: cualquier usuario de la lista de "Test users" puede autorizar. El refresh token **expira cada 7 días** si no se usa. Para equipos chicos o uso personal es lo más simple.
> - *In production*: el refresh token no expira por tiempo. Requiere verificación de Google si pedís scopes sensibles (ésta app no los pide, así que podés publicar sin verificación).

---

## Paso 4 — Crear el OAuth Client (Desktop app)

1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. *Application type:* **Desktop app** (crítico — **no uses Web application**).
3. *Name:* `google-ads-mcp-desktop` o similar.
4. **Create**. Aparece un modal con el Client ID y Client Secret.
5. Click **Download JSON**. Se descarga `client_secret_<xxxx>.apps.googleusercontent.com.json`.
6. Guardalo en un lugar privado — por ejemplo `~/.google-ads/client_secret.json` (Linux/macOS) o `C:\Users\<vos>\.google-ads\client_secret.json` (Windows). **No lo subas a Git.**

---

## Paso 5 — Developer Token de Google Ads

El Developer Token identifica tu integración ante la API de Google Ads. Hay tres niveles:

| Nivel | Qué cuentas podés consultar | Cómo se obtiene | Para qué sirve |
| --- | --- | --- | --- |
| **Test access** | Solo [cuentas test](https://developers.google.com/google-ads/api/docs/best-practices/test-accounts). Datos sintéticos. | Inmediato (al crear el token en API Center). | Desarrollo y pruebas sin datos reales. |
| **Basic access** | Hasta **15 000 operaciones/día** sobre cuentas de producción. | [Formulario de solicitud](https://support.google.com/adspolicy/contact/new_token_application). Tarda 1-3 días. | Uso personal o agencia chica. |
| **Standard access** | Sin tope (sujeto a cuotas globales). | Upgrade desde Basic, con auditoría de uso. Tarda 1-3 semanas. | Producción, agencias medianas/grandes. |

### Cómo generar el token

1. Entrá a tu **cuenta de administración (MCC)** de Google Ads con la cuenta de Google que autorizaste en el OAuth consent screen.
2. Arriba a la derecha: **Tools → API Center** (debajo de *Setup*).
3. Aceptá los términos si es la primera vez.
4. Vas a ver:
   - **Developer token**: la clave string (algo tipo `ABCDefGhIJkl_MNO-PqR`).
   - **Access level**: empieza en *Test*.
5. Si querés producción, apretá **Apply for Basic access** y completá el formulario (van a preguntarte caso de uso, URL pública, política de privacidad).

Guardá el token — vas a ponerlo como variable de entorno o lo detecta el `connect` si lo pedís con `--developer-token`.

> **Si no tenés un MCC**: podés crear uno gratis en [ads.google.com](https://ads.google.com/home/tools/manager-accounts/) y vincular tu cuenta directa. El Developer Token siempre se emite desde un MCC, incluso si lo usás para una cuenta directa.

---

## Paso 6 — Conectar la cuenta al cliente MCP (`connect`)

Teniendo listos:
- `client_secret.json` (del Paso 4)
- Developer token (del Paso 5)
- El Customer ID de la cuenta que querés consultar (10 dígitos, visible arriba a la derecha en Google Ads UI, sin guiones)

### Cuenta directa

```bash
google-ads-mcp-server connect \
    --client claude-desktop \
    --customer 1234567890 \
    --oauth-client ~/.google-ads/client_secret.json \
    --developer-token TU_DEV_TOKEN
```

El comando:
1. Imprime una URL — abrí el browser, ingresá con la cuenta Google que agregaste como Test user.
2. Google pide confirmación: *"google-ads-mcp quiere acceder a Google Ads"* → **Allow**.
3. El servidor captura el redirect en loopback, canjea el código por tokens, y guarda:
   - `~/.google-ads/token.json` (refresh token cifrado por el sistema operativo si está disponible)
   - El perfil en `~/.google-ads/profiles/<customer>.json`
   - La configuración en `claude_desktop_config.json` (o equivalente del cliente elegido)
4. Verifica contra la API que las credenciales funcionan y que la cuenta devuelve datos.

### Cuenta bajo MCC (una o varias)

```bash
# Una cuenta bajo un MCC
google-ads-mcp-server connect \
    --client codex --manager 9999999999 --customer 1234567890 \
    --oauth-client ~/.google-ads/client_secret.json --developer-token TU_DEV_TOKEN

# Todas las cuentas activas bajo un MCC (auto-discovery)
google-ads-mcp-server connect \
    --client antigravity --manager 9999999999 --all-mcc-clients --preset agency \
    --oauth-client ~/.google-ads/client_secret.json --developer-token TU_DEV_TOKEN
```

### Reusar el perfil en otro cliente

Una vez tenés el perfil guardado, agregarlo a otro cliente no pide OAuth de nuevo:

```bash
google-ads-mcp-server register --client gemini-cli --preset leads
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
| Antigravity | `antigravity` | Agrega a `~/.gemini/antigravity/mcp_config.json` |
| Gemini CLI | `gemini-cli` | Agrega a `~/.gemini/settings.json` |
| Cursor / VS Code / Cherry Studio / Cline / Kimi / otros | `generic` | Imprime el JSON `{"mcpServers": ...}` por stdout |

---

## Paso 7 — Verificar (`doctor`)

```bash
google-ads-mcp-server doctor            # humano, con llamada a la API
google-ads-mcp-server doctor --offline  # solo config local, sin red
google-ads-mcp-server doctor --json     # salida para CI/scripts
```

`doctor` separa claramente:
- **Credencial inválida** vs. **permiso faltante** vs. **límite de access level** vs. **cuenta sin datos recientes**
- Currency y timezone detectados
- Versión de API vs. la soportada por el servidor

Si todo está verde, abrí tu cliente (reiniciá Claude Desktop) y probá:
- *"Corré `server_status` y después `list_accounts`"*
- *"Dame el `get_campaign_performance` de los últimos 30 días en la cuenta `1234567890`"*

---

## Troubleshooting

### `invalid_grant` o `Token has been expired or revoked`
- **Causa más común:** la OAuth consent screen está en *Testing* y el refresh token quedó inactivo más de 7 días.
- **Fix:** volvé a correr `google-ads-mcp-server connect ...`. O pasá la app a **In production** (no requiere verificación si no pedís scopes sensibles).

### `USER_PERMISSION_DENIED` o `DEVELOPER_TOKEN_NOT_APPROVED`
- El Developer Token no tiene permiso sobre esa cuenta.
- Si estás en *Test access*, solo funcionan [cuentas test](https://developers.google.com/google-ads/api/docs/best-practices/test-accounts). Creá una en Google Ads o solicitá *Basic access*.
- Si estás en *Basic*, verificá que la cuenta de Google autorizada tenga permiso Read-Only o superior sobre el Customer ID.

### `access_denied` al autorizar en el browser
- La cuenta Google que estás usando **no está en la lista de Test users** de la OAuth consent screen. Agregala en Google Cloud → OAuth consent screen → Test users.

### `OAuth client was not found`
- El JSON que pasaste a `--oauth-client` quedó inválido o apunta a un cliente borrado. Volvé a descargarlo desde Credentials.

### `customer not found` o `CUSTOMER_NOT_ENABLED`
- El Customer ID tiene guiones (`XXX-XXX-XXXX`): pasalo sin guiones (`XXXXXXXXXX`).
- La cuenta está suspendida o cancelada: verificá en Google Ads UI.

### `doctor` dice `queries return no rows` pero la cuenta tiene datos
- Timezone de la cuenta vs. la query: el servidor usa `complete_days_only=True` en el timezone de la cuenta — si recién arrancó el día allá, podés no ver nada aún.
- Access level: cuentas de **Test access** solo tienen datos sintéticos limitados.

### Windows: rutas con espacios
Encerrá en comillas y escapá las barras:
```powershell
google-ads-mcp-server connect --client claude-desktop `
    --customer 1234567890 `
    --oauth-client "C:\Users\Mi Usuario\.google-ads\client_secret.json" `
    --developer-token TU_DEV_TOKEN
```

---

## Dónde se guarda cada cosa

| Ruta | Qué contiene |
| --- | --- |
| `~/.google-ads/client_secret.json` | Credenciales OAuth del Cloud project. **Nunca commitear.** |
| `~/.google-ads/token.json` | Refresh token (cifrado cuando el SO lo permite). **Nunca commitear.** |
| `~/.google-ads/profiles/<customer>.json` | Perfil del servidor (customer IDs permitidos, preset, módulos). |
| `claude_desktop_config.json` (o equivalente) | Entrada `mcpServers` con comando, args y env vars del profile. |
| `./data/audit*.jsonl` | Log append-only de llamadas a la API (gitignoreado por default). |

> **Variables de entorno equivalentes** (útiles si corrés el servidor sin el CLI `connect`):
> - `GOOGLE_ADS_CLIENT_ID`, `GOOGLE_ADS_CLIENT_SECRET`
> - `GOOGLE_ADS_DEVELOPER_TOKEN`
> - `GOOGLE_ADS_LOGIN_CUSTOMER_ID` (el MCC, si aplica)
> - `GOOGLE_ADS_TOKEN_PATH` (ruta al `token.json`)
> - `ALLOWED_CUSTOMER_IDS` (whitelist CSV)
> - `READ_ONLY_MODE=true` (recomendado siempre salvo mutaciones)

---

## Siguientes pasos

- Revisá [`guardrails.md`](guardrails.md) para entender los controles de seguridad del modo escritura.
- Si vas a operar múltiples cuentas, mirá [`compatibility-matrix.md`](compatibility-matrix.md) y [`remote-pilot.md`](remote-pilot.md).
- Para pre-flights automatizados en CI: `google-ads-mcp-server doctor --json` sale con exit code distinto de 0 si algo falla.
