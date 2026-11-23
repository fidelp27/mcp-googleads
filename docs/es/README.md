# Google Ads MCP Server — Guía rápida en español

Servidor MCP open source, de **solo lectura por defecto**, diseñado para auditar, diagnosticar, reportar y operar cuentas de Google Ads desde apps de IA de escritorio (**Claude Desktop, Claude Code, Codex, Antigravity, Gemini CLI, clientes con DeepSeek/Kimi, Cursor, VS Code**).

---

## 1. Probar en 30 segundos sin credenciales (`--demo`)

```bash
pip install google-ads-mcp-server
# Registrar modo demo en tu cliente (sin Google Cloud ni permisos):
google-ads-mcp-server register --client claude-desktop --demo
# Otros clientes: claude-code, codex, antigravity, gemini-cli, generic
```

El modo `--demo` incluye dos cuentas sintéticas realistas (`1111111111` ecommerce en `EUR` y `2222222222` B2B Leads en `USD`) con campañas Search y PMax, gasto desperdiciado, objetivos de conversión y alertas.

> **¿No sabés qué preguntarle?** Mirá
> [**¿Qué puedo preguntarle? — Prompts de ejemplo por alcance y nivel**](que-puedo-preguntar.md)
> para una guía en lenguaje simple por caso de uso (ecommerce, leads, agencia,
> usuario avanzado).

---

## 2. Conectar cuentas reales (`connect` + `doctor`)

```bash
# Cuenta directa
google-ads-mcp-server connect --client antigravity --customer 1234567890 --oauth-client /ruta/client_secret.json

# Todas las cuentas activas bajo un MCC
google-ads-mcp-server connect --client codex --manager 9999999999 --all-mcc-clients --oauth-client /ruta/client_secret.json

# Preflight para verificar credenciales, nivel de acceso, zona horaria y datos
google-ads-mcp-server doctor
```

Consultá todos los detalles de Google Cloud OAuth en [docs/connect.md](../connect.md).

---

## 3. Elegir el preset adecuado por sesión

Por defecto el servidor carga solo `core` (**25 herramientas** y **0 herramientas de escritura**):

- `--preset core`: visión general, tendencias diarias/semanales, reportes, waste, alertas y perfiles de análisis.
- `--preset leads`: suma `diagnostics`, `measurement` y `search` (conversiones, entrega, pacing, keywords, search terms, RSAs).
- `--preset ecommerce`: suma `diagnostics`, `measurement`, `pmax` y `shopping` (asset groups, SKUs, ROAS, placements).
- `--preset agency`: suma `diagnostics`, `measurement`, `search`, `pmax` y `mcc` (multi-cuenta y multi-moneda).

---

## 4. Prompts listos para copiar según tu rol

### Particular / Dueño de negocio
> *"Revisá el ritmo de gasto mensual de mi cuenta (`get_budget_pacing`), decime cuánto voy a gastar a fin de mes en mi moneda local y listá los términos de búsqueda que gastaron sin convertir en los últimos 30 días."*

### Analista
> *"Traé la tendencia diaria de los últimos 14 días cerrados (`get_performance_trends`) comparando `conversions` vs `conversions_by_conversion_date`, e indicá cualquier aviso de conversion lag."*

### Especialista PPC
> *"Ejecutá `diagnose_delivery` para explicar por qué cayó la entrega separando causas confirmadas de hipótesis. Luego auditá Performance Max (`get_pmax_diagnostics`) y productos Shopping sin ventas (`get_shopping_performance`)."*

### Responsable de medición
> *"Corré `audit_conversion_tracking` y `get_offline_conversion_diagnostics`. Explicame qué acciones impulsan realmente las pujas de cada campaña, si la propiedad está centralizada en un MCC y si conviene migrar a Data Manager API."*

### Agencia multi-cuenta
> *"Cargá el perfil `agency-weekly`, generá el dashboard consolidado manteniendo las monedas separadas (`generate_agency_dashboard`) y revisá alertas con `check_alerts`."*

---

## 5. Alertas programadas por CLI

```bash
google-ads-mcp-server alerts --profile leads-audit --format markdown --fail-on-alerts
```

Útil para cron, GitHub Actions o webhooks de Slack/email. Devuelve código de salida `2` cuando se dispara al menos una alerta.
