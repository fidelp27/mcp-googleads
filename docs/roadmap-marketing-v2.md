# Plan de ejecución v2 — Google Ads MCP (Open Source, Multi-IA)

## 1. Visión y posicionamiento

El objetivo es construir el servidor **Google Ads MCP open source más completo, confiable y fácil de adoptar**, pensado para funcionar con precisión desde clientes MCP de escritorio y CLI con los principales ecosistemas de IA (**Anthropic/Claude, OpenAI/Codex, Google/Gemini, DeepSeek y Kimi**) y una configuración genérica estándar para cualquier otro cliente.

- **Distribución v1.0 (escritorio primero):** Open source, autogestionada, **solo lectura por defecto (`READ_ONLY_MODE=true`)** y **`stdio` como camino principal**. `Streamable HTTP` autenticado se conserva como opción para usuarios técnicos que quieran autogestionarlo; OAuth 2.1 remoto, despliegues cloud y apps web/móviles pasan a la entrega posterior (v1.1 / Cloud) para no retrasar el lanzamiento de la v1.0.
- **Diferencial frente al MCP oficial de Google Ads:** Auditorías operativas 360°, **análisis personalizables** por cuenta/vertical (Markdown + TOML), catálogo modular por canal, cobertura real de medición y entrega, soporte multi-MCC/multi-moneda y un motor de escrituras seguras con `validateOnly`, diff, aprobación humana, control de estado obsoleto y reversión.
- **Puerta abierta a comercialización futura:** La arquitectura del núcleo se desacopla de `.env` y archivos locales mediante interfaces de contexto y almacenamiento (`RuntimeContext` / `StorageBackend`), permitiendo lanzar más adelante una versión remota/cloud (OAuth en 1 clic sin Google Cloud, multi-tenant, equipos y alertas) reutilizando el 100% del motor open source.

---

## 2. Resultados que debe cubrir el producto

| Perfil | Trabajo que debe poder completar |
| --- | --- |
| **Particular / Dueño de negocio** | Probar el MCP en <1 minuto con modo demo, conectar su cuenta en <15 minutos desde su cliente de escritorio, entender cuánto gasta vs. presupuesto mensual, detectar gasto desperdiciado y saber si sus conversiones están bien medidas. |
| **Analista** | Comparar períodos completos en la zona horaria y moneda de la cuenta, ver series diarias/semanales con aviso de *conversion lag*, reutilizar perfiles de análisis reproducibles y exportar gasto, resultados y valor. |
| **Especialista de campañas** | Explicar por qué una campaña no entrega o cayó de rendimiento (facturación, políticas, presupuesto, pujas, audiencias, URLs, feed), auditar Search/PMax/Shopping/Display/Demand Gen y aplicar cambios verificables y reversibles. |
| **Responsable de medición** | Saber exactamente qué eventos y objetivos (`campaign`/`customer`/`custom_conversion_goal`) impulsan las pujas, auditar Enhanced Conversions, verificar cargas offline y conciliar discrepancias sin falsos positivos. |
| **Agencia** | Alternar cuentas directas y múltiples MCC sin reiniciar el servidor, aplicar plantillas de auditoría propias por cliente/vertical, consolidar dashboards separando monedas, ejecutar alertas por CLI, operar lotes con reintento parcial y mantener auditoría por cuenta y operador. |

---

## 3. Compatibilidad multi-IA y matriz de clientes (v1.0)

Para mantener el proyecto enfocado y honesto, la v1.0 prioriza **combinaciones concretas y probadas de escritorio (`stdio`)** más una configuración **`generic`** estándar, publicando una **matriz verificable por aplicación, versión, sistema operativo y transporte**.

| Ecosistema / Modelo | Combinación probada en v1.0 | Transporte v1.0 | Instalación / registro | Notas técnicas |
| --- | --- | --- | --- | --- |
| **Anthropic (Claude)** | Claude Desktop (y Claude Code) | `stdio` | `--client claude-desktop`, `--client claude-code` + empaquetado `.mcpb` | Aprovecha `ToolAnnotations`, prompts y `resources`; sensible a costo/latencia con demasiadas tools. |
| **OpenAI** | Codex (CLI / Desktop / IDE) | `stdio` | `--client codex` | Sensible a longitud de descripciones y cantidad de tools; usa `ToolAnnotations` para confirmaciones. |
| **Google (Gemini)** | Antigravity y Gemini CLI | `stdio` | `--client antigravity`, `--client gemini-cli` | Estricto con JSON Schema: evitar `dict[str, Any]`, uniones complejas (`anyOf` anidados) y `additionalProperties` ambiguos. |
| **DeepSeek** | Cliente de escritorio concreto (ej. Cherry Studio o Cline) configurado con API de DeepSeek | `stdio` | Guía verificada + `--client generic` | La API de DeepSeek ejecuta las llamadas a herramientas y el cliente de escritorio establece la conexión MCP; requiere esquemas planos, pocas tools activas y respuestas compactas. |
| **Kimi (Moonshot)** | Kimi CLI o cliente de escritorio concreto (ej. Cherry Studio) configurado con Kimi | `stdio` | Guía verificada + `--client generic` | Mismas exigencias de *tool calling*: catálogo modular acotado, esquemas limpios y sin depender de MCP `resources`/`prompts`. |
| **Otros clientes MCP** | Cursor, VS Code, Windsurf, Open WebUI, LibreChat, etc. | `stdio` (o `Streamable HTTP` autogestionado) | `--client generic` (bloque JSON estándar) | Cualquier cliente estándar puede conectarse sin esperar un instalador dedicado. |

> **Nota sobre acceso remoto:** `Streamable HTTP` con autenticación opcional (Bearer/proxy) se conserva documentado para usuarios técnicos que autogestionan su instancia, pero **OAuth 2.1 remoto, despliegues cloud y soporte oficial para ChatGPT web / Claude.ai web y apps móviles quedan fuera de los criterios de salida de la v1.0** (ver Sección 9).

### Reglas de interoperabilidad obligatorias para todas las tools
1. **Esquemas JSON Schema universales:** Prohibido exponer parámetros opacos (`dict[str, Any]` sin modelo tipado). Usar modelos Pydantic planos validados en CI con un linter de compatibilidad para Claude, OpenAI, Gemini, DeepSeek y Kimi.
2. **Autocontención de tools:** Ninguna regla crítica (flujo `dry_run -> approval_id`, moneda, *conversion lag*, límites) puede depender de que el cliente lea `resources` o `prompts` MCP; debe estar explícita en la descripción y respuesta de la tool.
3. **Presupuesto de contexto (anti-desborde):** Todas las lecturas incluyen `limit` por defecto razonable (ej. 50–200 filas ordenadas por gasto/impacto), devuelven resumen agregado primero y marcan `truncated: true` + `total_rows`, evitando volcar decenas de miles de filas al modelo.
4. **Salida estructurada consistente:** Unificar el formato entre `google_ads_server.py` y `agency/` (JSON compacto + soporte de `structuredContent` según la especificación MCP), reemplazando tablas ASCII extensas.

---

## 4. Diseño de catálogo modular y análisis personalizables

### 4.1 Catálogo modular por dominio y presets
En lugar de exponer las 124 herramientas de golpe, el usuario elige qué módulos cargar en cada sesión o perfil, manteniendo un conjunto activo pequeño (~20–30 tools):

- **Módulo base (siempre activo por defecto):** `core` (25 tools: estado, cuentas, resumen de rendimiento, tendencias diarias/semanales, reportes, dashboard multi-moneda, waste, alertas, perfiles de análisis y GAQL seguro).
- **Módulos optativos de lectura/diagnóstico:** `diagnostics`, `measurement`, `search`, `pmax`, `shopping`, `display`, `demand-gen`, `mcc`.
- **Módulo optativo de escritura:** `operations` (68 tools; solo disponible cuando `READ_ONLY_MODE=false`; si `READ_ONLY_MODE=true`, las tools de escritura **no se registran** en `tools/list`).
- **Presets de 1 palabra** para simplificar la configuración inicial:
  - `--preset leads` → `core,diagnostics,measurement,search`
  - `--preset ecommerce` → `core,diagnostics,measurement,pmax,shopping`
  - `--preset agency` → `core,diagnostics,measurement,search,pmax,mcc`
  - `--preset full` → todos los módulos de lectura (más `operations` solo si escritura está habilitada)
  - Selección fina vía `--modules core,diagnostics,measurement,pmax` o declarada en el perfil de cuenta/proyecto.

### 4.2 Análisis personalizables (Markdown + TOML)
Permite que cada particular, analista o agencia adapte las auditorías y reportes a su metodología sin tocar código:

- **Plantillas Markdown (`*.md`):** Definen instrucciones, criterio analítico, orden de prioridades y estructura del informe (por ejemplo: *auditoría de leads* que revisa objetivos de puja, separa conversiones primarias y secundarias y ordena hallazgos por impacto; o *auditoría ecommerce PMax/Shopping* centrada en ROAS por producto y fuga de gasto).
- **Perfiles TOML (`*.toml`):** Definen parámetros reproducibles por cuenta o proyecto usando `tomllib` (stdlib de Python 3.11+): rango de fechas, campañas incluidas/excluidas, CPA/ROAS objetivo, presupuesto mensual esperado, módulos sugeridos y umbrales de alerta (unificados con la configuración de alertas para no duplicar fuentes de verdad). Cada informe declara explícitamente qué perfil y parámetros utilizó.
- **Exposición doble (compatible con todos los clientes):**
  1. Como **prompt MCP** seleccionable en clientes que soportan prompts (ej. Claude Desktop / Claude Code).
  2. Mediante tools dedicadas (`list_analysis_profiles`, `get_analysis_profile`) para clientes que solo consumen herramientas (ej. configuraciones con DeepSeek, Kimi u OpenAI).
- **Frontera de seguridad estricta:** Los perfiles controlan únicamente preferencias de análisis y formato. Los permisos de cuenta (`ALLOWED_CUSTOMER_IDS`), `READ_ONLY_MODE`, límites económicos y aprobaciones siempre se aplican en el servidor y no pueden ser anulados por un perfil.

---

## 5. Línea base actual y deuda técnica a resolver primero

El repositorio parte de una base avanzada (**124 herramientas MCP**, motor de guardarraíles, recursos/prompts embebidos, flujo `google-ads-mcp-connect` y **478 tests**), pero presenta bloqueantes claros antes de abrirlo al público:

1. **Nombre del paquete y empaquetado:** Verificar y reservar el nombre definitivo en PyPI (hoy `pyproject.toml` declara `google-ads-mcp`, que puede colisionar con el paquete oficial o uno existente) y reparar el test `tests/test_protocol.py::test_claude_desktop_launcher_lists_tools`.
2. **Limpieza de datos privados:** Retirar datos reales de clientes del repositorio y revisar el historial Git (`config/clients.json`, `config/branding.json`, PDFs internos en `output/pdf/`, ejemplos en `docs/`/`tests/` y archivos locales de análisis), reemplazando por plantillas `*.example.json` / `*.example.toml`.
3. **Bugs confirmados en el código actual:**
   - `agency/reports.py` (`check_alerts`): al llamarse sin `customer_ids`, toma la clave `"defaults"` de `config/alerts.json` como si fuera un `customer_id` y falla.
   - `agency/reports.py` (`generate_agency_dashboard`): suma `cost_micros` y `conversions_value` entre clientes sin separar por moneda.
   - `agency/read_tools.py` (`check_consent_signal_coverage`): interpreta `conversions / all_conversions < 30%` como fuga de Consent Mode/tag, generando falsos positivos en cuentas con conversiones secundarias.
   - Fechas y zonas horarias: usa `date.today()` local (incluyendo el día actual incompleto) en lugar de días cerrados en la zona horaria de la cuenta, y omite metadatos (`currency_code`, `time_zone`, `queried_at_utc`).
   - `Dockerfile`: omite copiar `google_ads_connect.py` pese a declararlo en `pyproject.toml`.
4. **Catálogo plano y sin límite de contexto:** 124 tools planas (con escrituras visibles en modo lectura y solapamientos como `execute_gaql_query` vs. `run_gaql`) y `agency/api.py` (`search()`) paginando todas las filas sin tope.
5. **Motor de escritura fragmentado:** `experiment_tools.py`, `demand_gen_tools.py`, `offline_conversions_tools.py`, `customer_match_tools.py` y `mcc_tools.py` no pasan por `agency/mutations.py` y ninguna mutación valida todavía si el estado en Google Ads cambió desde el `dry_run`.
6. **Único MCC global:** `GOOGLE_ADS_LOGIN_CUSTOMER_ID` global impide alternar cuentas directas y múltiples MCC en una misma sesión.

---

## 6. Plan de ejecución por fases (12 a 14 semanas)

Estimación para un desarrollador dedicado con apoyo semanal de un especialista de marketing y acceso a cuentas piloto (leads, ecommerce y agencia MCC).

| Fase | Estado | Ventana | Alcance principal | Condición de salida |
| --- | --- | --- | --- | --- |
| **0. Instalación de escritorio, higiene y bugs** | ✅ **Código + CI completos** *(pendiente publicación PyPI/purga historial)* | Semanas 1–2 | Definir y reservar el nombre final del paquete en PyPI (`google-ads-mcp-server`). Limpiar datos privados del repo y revisar historial Git. Corregir bugs de `check_alerts`, dashboard multi-moneda, `check_consent_signal_coverage`, `Dockerfile` y test de protocolo. Completar `connect` (`claude-desktop`, `claude-code`, `codex`, `antigravity`, `gemini-cli`, `generic`), `doctor` y `--demo` sin credenciales. Configurar CI multiplataforma (Windows/macOS/Linux). | Cualquier persona ejecuta el modo `--demo` en <1 minuto. Dos usuarios externos instalan sin ayuda en escritorio (`stdio`), conectan su cuenta con `connect` + `doctor` y obtienen su primer reporte en <15 minutos. CI en verde en los 3 SO. |
| **1. Catálogo modular, análisis personalizables y fundación de datos** | ✅ **Completada** | Semanas 3–4 | Implementar catálogo modular (`core` + `diagnostics`, `measurement`, `search`, `pmax`, `shopping`, `display`, `demand-gen`, `mcc`, `operations`) y presets; ocultar escrituras en `tools/list` cuando `READ_ONLY_MODE=true`. Implementar **perfiles de análisis personalizables** (Markdown + TOML + prompt MCP y tools). Modelos Pydantic planos + **linter de esquemas multi-IA**, tope de filas (`max_rows`/`truncated: true`). Estandarizar `currency_code`, `time_zone`, días cerrados, series `total/daily/weekly`, `conversions_by_conversion_date`, mapa dinámico multi-MCC y `RuntimeContext` / `StorageBackend`. | El modo default expone 25 tools (≤25) que pasan el linter multi-IA; ninguna lectura desborda el contexto; perfiles TOML/Markdown reproducibles; cifras alineadas a zona horaria/moneda; multi-MCC + cuentas directas en una misma sesión. |
| **2. Medición y auditoría de conversiones** | ✅ **Completada** | Semanas 5–6 | Crear `audit_conversion_tracking`: unificar `conversion_action`, `customer_conversion_goal`, `campaign_conversion_goal`, `custom_conversion_goal`, propietario MCC, estrategia de puja y campañas afectadas. Reemplazar el proxy de consentimiento por análisis transparente de `conversions` vs. `all_conversions`. Incorporar `get_offline_conversion_diagnostics` (sin PII) y detección de elegibilidad para Data Manager API. | Una sola auditoría explica qué eventos guían las pujas de cada campaña (estándar, personalizado con secundaria, MCC), distingue envío/aceptación/atribución y detecta configuraciones rotas sin falsos positivos. |
| **3. Diagnóstico 360° y cobertura por canal** | ✅ **Completada (3A + 3B)** | Semanas 7–10 | **Bloque 3A:** `diagnose_delivery` (facturación, `account_budget`, `policy_summary`, estados, presupuesto, ranking), `get_budget_pacing`, `get_landing_page_performance`, `get_demographics_performance`, Quality Score desglosado, `get_assets_and_calls_performance` y CLI ejecutable `alerts`.<br>**Bloque 3B:** `get_pmax_diagnostics`, `get_shopping_performance`, `get_placements_and_video_performance`, `analyze_auction_insights_csv` (EN/ES) y perfil `agency-weekly`. | Un ecommerce detecta productos/asset groups con fuga de gasto; un negocio de leads explica caídas de entrega o CPA con evidencia separada en causas confirmadas e hipótesis; ambos cubren pacing, landing pages, demografía y placements sin salir del MCP. |
| **4. Escritura segura unificada y lotes** | ✅ **Completada** | Semanas 11–12 | **Optimistic locking** (`verify_not_stale` / `state_verifier`) en `agency/mutations.py`. Escrituras prioritarias faltantes en `operations`: `update_campaign_conversion_goal`, `set_conversion_action_primary`, `exclude_placements` y `add_account_negative_keywords`. Reintento parcial (`retry_failed_from_approval_id`) en `mcc_bulk_apply`. Reversión ampliada en `revert_operation` (keyword bids/estados, `primary_for_goal` y objetivos de campaña). | Ningún cambio se ejecuta fuera de cuenta/caps ni sobre estado modificado tras el `dry_run`. Un fallo parcial en lote reintenta únicamente los ítems fallidos sin duplicar los exitosos. |
| **5. Pilotos, matriz de compatibilidad y lanzamiento v1.0** | 🟡 **Docs y checklist completos** *(pendiente pilotos reales y publicación)* | Semanas 13–14 | Publicar `README.md` (EN), `docs/es/README.md`, `docs/compatibility-matrix.md`, `DESIGN.md`, `.env.example` y `docs/release-checklist.md`. Ejecutar 3 pilotos reales (Leads, Ecommerce, Agencia MCC), purgar historial Git previo y publicar v1.0 en GitHub, PyPI (`google-ads-mcp-server`), GHCR y registros MCP. | Los 3 pilotos y combinaciones de la matriz completan sus recorridos; cero datos privados en el historial público; paquete instalable por `uvx`. |

---

## 7. Backlog priorizado inmediato (Sprints 1 y 2)

| Prioridad | Tarea | Criterio de cierre verificable |
| --- | --- | --- |
| **P0** | ✅ **Completado** — Nombre del paquete definido en `pyproject.toml` (`google-ads-mcp-server`, libre en PyPI) + limpieza de datos reales del repo (`config/*.example.json`, `git rm --cached` del PDF interno, sanitización de `docs/`/`scripts/`/`tests/` y `tests/test_hygiene.py`). *(Pendiente solo purga de historial Git y publicación inicial en PyPI antes de abrir el repo).* | `google-ads-mcp-server` libre en PyPI, `git grep` con 0 coincidencias y `tests/test_hygiene.py` en verde. |
| **P0** | ✅ **Completado** — Corregir bugs existentes: `check_alerts`, dashboard multi-moneda, `check_consent_signal_coverage`, `Dockerfile` y test `test_claude_desktop_launcher_lists_tools`. | `pytest` 100% verde (`487 passed`), wheel verificado sobre el contexto exacto de `Dockerfile` y tests de regresión en `tests/test_reports_bugs.py` y `tests/test_consent_coverage.py`. |
| **P0** | ✅ **Completado** — Completar `connect` (`claude-desktop`, `claude-code`, `codex`, `antigravity`, `gemini-cli`, `generic` + multi-cuenta/`--all-mcc-clients`), `doctor` (preflight), modo `--demo` sin credenciales y CI multiplataforma (`.github/workflows/ci.yml` en Ubuntu/macOS/Windows + wheel/Docker + privacy-guard). | `tests/test_demo_and_doctor.py` + CI definido para 3 SO + arranque `--demo` E2E en <1s sin credenciales. |
| **P1** | ✅ **Completado** — Implementar perfiles de análisis personalizables (Markdown + TOML en `config/profiles/`, prompt MCP `analysis_profile_prompt` + tools `list_analysis_profiles` / `get_analysis_profile`, unificados con umbrales de `alerts.json` y subordinados a guardarraíles del servidor). | 2 perfiles reproducibles (`leads-audit` y `ecommerce-audit`) verificados por tool y prompt en `tests/test_phase1_catalog_profiles.py`. |
| **P1** | ✅ **Completado** — Implementar catálogo modular (`--modules` / `--preset` en `agency/modules.py`), ocultar escrituras (`operations`) en `READ_ONLY_MODE=true`, añadir tope de filas (`max_rows`/`truncated: true` en `agency/api.py` y `agency/read_tools.py`), tipar `ReportBranding` y añadir linter de JSON Schema multi-IA. | `tools/list` en modo default expone **25 tools** (≤25), 0 tools de escritura y 0 errores en el linter multi-IA (`514 passed`). |
| **P1** | ✅ **Completado** — Estandarizar zona horaria de cuenta (`agency/dates.py` + `tzdata` en Windows), días cerrados por defecto (`complete_days_only=True`), metadatos de cobertura y *conversion lag*, series temporales (`get_performance_trends` con `total/daily/weekly` + `conversions_by_conversion_date`), mapa dinámico `customer_id -> login_customer_id` multi-MCC (`agency/runtime.py` + `agency/api.py`) y `RuntimeContext` / `StorageBackend`. | `tests/test_phase1_runtime_dates.py` en verde (`514 passed` total): rangos exactos en 2 zonas horarias (`Europe/Madrid` y `America/New_York`), multi-MCC + cuentas directas en una misma sesión y tendencias diarias/semanales verificadas. |
| **P2** | ✅ **Completado** — `audit_conversion_tracking`, `get_offline_conversion_diagnostics` y `diagnose_delivery` (con billing, `account_budget`, `policy_summary`), más `get_budget_pacing`, landing pages, demografía, Quality Score desglosado, assets/calls y CLI ejecutable `alerts`. | Cubre los 3 casos de objetivos, separa `confirmed_causes`/`hypotheses`/`insufficient_data`, CLI `alerts` validado y linter multi-IA en verde (`523 passed`). |
| **P2** | ✅ **Completado** — Cobertura de canales (Fase 3B): `get_pmax_diagnostics`, `get_shopping_performance`, `get_placements_and_video_performance`, `analyze_auction_insights_csv` (EN/ES) y perfil `agency-weekly`. | `tests/test_phase3b_channels.py` + linter multi-IA sobre todos los módulos de lectura (`526 passed`). |
| **P2** | ✅ **Completado** — Escrituras seguras y lotes (Fase 4): *optimistic locking* (`verify_not_stale`), `update_campaign_conversion_goal`, `set_conversion_action_primary`, `exclude_placements`, `add_account_negative_keywords`, reintento parcial en `mcc_bulk_apply` y reversión ampliada. | `tests/test_phase4_operations.py` en verde (`531 passed`), todas ocultas en `READ_ONLY_MODE=true`. |
| **P2** | ✅ **Completado (docs/empaquetado)** — Fase 5: `README.md` (EN), `docs/es/README.md`, `docs/compatibility-matrix.md`, `DESIGN.md`, `.env.example` y `docs/release-checklist.md`. *(Pendiente solo pilotos reales y publicación externa).* | `531 passed` + wheel `google_ads_mcp_server-0.2.0-py3-none-any.whl` construido limpio. |

---

## 8. Reglas de validación y calidad

- **Interoperabilidad multi-IA:** Toda tool debe pasar el linter automático de JSON Schema (sin tipos opacos), incluir `ToolAnnotations` precisas, devolver respuestas acotadas en tamaño y ser utilizable sin depender de `resources` ni `prompts`.
- **Perfiles y reproducibilidad:** Todo informe generado con un perfil declara el nombre/versión del perfil y los parámetros TOML efectivos. Un perfil nunca puede modificar `READ_ONLY_MODE`, `ALLOWED_CUSTOMER_IDS`, límites económicos ni requisitos de aprobación.
- **Lecturas y exactitud de datos:** Usar cuentas de prueba para contratos/permisos, fixtures sintéticos (y modo `--demo`) para regresiones, y cuentas piloto autorizadas para contrastar cifras contra la UI de Google Ads (misma moneda, zona horaria, rango cerrado, columnas y filtros). Nunca mezclar monedas en agregados sin conversión explícita. No presentar `conversions / all_conversions` como prueba de consentimiento.
- **Escrituras y guardarraíles:** Toda mutación muestra cuenta, recurso, diff esperado, impacto sobre gasto y alcance de `validateOnly`. Antes de ejecutar `dry_run=false`, comprobar vigencia del `approval_id`, coincidencia exacta del payload y que el estado relevante en Google Ads no haya cambiado. Guardar actor, resultado, `audit_id` y `previous_state` redactando PII y secretos.
- **Seguridad y acceso:** En v1.0, `stdio` local es el recorrido principal. En `streamable-http` autogestionado, impedir el arranque en interfaces no locales (`0.0.0.0`) sin capa de autenticación configurada (salvo bandera explícita de desarrollo).
- **Bloqueantes de publicación:** Cualquier error de medición que induzca recomendaciones equivocadas, cualquier escritura aplicable fuera de la cuenta objetivo, cualquier esquema roto en la matriz de clientes soportados, cualquier filtración de credenciales/datos de clientes o fallo de instalación en Windows/macOS/Linux bloquea el release.

---

## 9. Entregas posteriores (post-v1.0)

Para mantener el foco y lanzar una v1.0 sólida de escritorio en 12–14 semanas, los siguientes bloques quedan planificados para v1.1+ según demanda real:

- **Acceso remoto oficial y apps web/móviles (v1.1):** Autenticación **MCP OAuth 2.1** (RFC 9728/8414 + PKCE), plantillas de despliegue cloud (Cloud Run, Railway, Docker+Caddy) y soporte verificado para **ChatGPT web/móvil**, **Claude.ai web/móvil** y clientes remotos de Gemini/DeepSeek/Kimi.
- **Integraciones de medición y feeds externos:** Escritor completo en **Data Manager API** para cargas offline, **Merchant API** (diagnóstico profundo de feed y rechazos de productos), **GA4** y **GTM** (verificación de disparo de etiquetas), y conciliación con CRM (adaptador CSV/JSON genérico primero, luego conectores como Zoho/HubSpot).
- **Canales y flujos especializados:** Promoción/graduación estadística de experimentos, creación completa de creatividades multi-formato en Demand Gen, y campañas **Video/YouTube**, **App** y **Hotel**.
- **Camino comercial opcional (Google Ads MCP Cloud):** Sobre el mismo `RuntimeContext`, ofrecer un servicio alojado multi-tenant con OAuth 1-clic sin configurar Google Cloud, aprobación de clientes por enlace web, alertas proactivas (Slack/Email/WhatsApp) y gestión de equipos — iniciando en paralelo los trámites de **Google OAuth Verification** y **Google Ads API Standard Access / RMF**.

---

## 10. Referencias técnicas

- [Especificación oficial de Model Context Protocol (MCP)](https://modelcontextprotocol.io/specification)
- [Extensiones locales MCP en Claude Desktop (`.mcpb`)](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop)
- [Objetivos de conversión por campaña](https://developers.google.com/google-ads/api/docs/conversions/goals/campaign-goals)
- [Migración de cargas offline a Data Manager API](https://developers.google.com/data-manager/api/devguides/events/google-ads/offline/upgrade)
- [Reportes de Performance Max](https://developers.google.com/google-ads/api/performance-max/reporting)
- [Niveles de acceso y Developer Token](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)
- [Mejores prácticas de pruebas en Google Ads API](https://developers.google.com/google-ads/api/docs/best-practices/testing)
- [MCP oficial de Google Ads (referencia de compatibilidad)](https://developers.google.com/google-ads/api/docs/developer-toolkit/mcp-server)

---

## 11. Bitácora de implementación y observaciones técnicas

### 2026-11-20 — Fase 0 · Tanda P0 (Corrección de 5 bugs confirmados + tests de regresión)

**Estado:** Completado (`487 passed` en `pytest`; empaquetado wheel validado sobre el contexto `COPY` del `Dockerfile`).

1. **`check_alerts` con `"defaults"` en `config/alerts.json` (`agency/reports.py`)**
   - **Qué se hizo:** Se extrajo `_resolve_alert_customer_ids()` para excluir la clave `"defaults"` de `alerts.json` y aplicar un orden explícito de resolución: `customer_ids` recibidos → IDs configurados en `alerts.json` (sin `"defaults"`) → `clients.json` → `ALLOWED_CUSTOMER_IDS` → `ValueError` accionable si no hay ninguna cuenta configurada.
   - **Por qué:** Al invocar `check_alerts()` sin parámetros con el `alerts.json` por defecto (`{"defaults": {...}}`), `list(alert_config)` devolvía `["defaults"]` y fallaba en `ensure_customer_allowed("defaults")`.

2. **Dashboard consolidado multi-moneda (`agency/reports.py`)**
   - **Qué se hizo:** Se añadió `_account_metadata()` (consulta `customer.currency_code` y `customer.time_zone` en `_report_data()`) y `_consolidate_by_currency()`. `generate_agency_dashboard()` ahora devuelve `currencies`, `mixed_currencies`, `managed_totals_by_currency` y `currency_code` en cada alerta; cuando hay una sola moneda mantiene `managed_totals` (compatibilidad hacia atrás), y cuando hay monedas mezcladas devuelve `managed_totals: null` + `currency_warning`. También se incluyó `currency_code` en `_markdown_report()`.
   - **Por qué:** Sumar `cost_micros` y `conversions_value` entre cuentas en distintas monedas (ej. ARS + EUR + USD) producía totales financieros inválidos y alertas ambiguas.

3. **Falso positivo de Consent Mode en `check_consent_signal_coverage` (`agency/read_tools.py`, `docs/conversions-recommendations.md`, `tests/test_consent_coverage.py`)**
   - **Qué se hizo:** Se corrigieron severidades, códigos y mensajes de alerta (`LOW_PRIMARY_CONVERSION_SHARE` en nivel `info`, `ZERO_PRIMARY_WITH_ALL_CONVERSIONS` y `PRIMARY_SHARE_DROP` en nivel `warning`), se agregó `account_primary_to_all_ratio` en `summary` conservando `account_attribution_rate` por compatibilidad, se actualizaron `notes` y la documentación en `docs/conversions-recommendations.md`, y se sanitizaron los fixtures del test (`1234567890` / `Demo Account`).
   - **Por qué:** En Google Ads, `conversions` incluye solo acciones primarias (usadas para pujar) mientras que `all_conversions` suma también secundarias (micro-conversiones, view-through). Un cociente `< 30%` es una diferencia de medición habitual y no prueba fuga de Consent Mode ni fallos de etiquetas.

4. **Empaquetado Docker incompleto (`Dockerfile`)**
   - **Qué se hizo:** Se agregó `google_ads_connect.py` a la instrucción `COPY` de `Dockerfile` y se añadió el test de regresión `test_dockerfile_copies_all_declared_py_modules` en `tests/test_reports_bugs.py`.
   - **Por qué:** `pyproject.toml` declara `py-modules = ["google_ads_server", "google_ads_connect"]`; omitir `google_ads_connect.py` hacía fallar `pip install .` al construir la imagen. Como Docker CLI no estaba instalado localmente, se validó construyendo el wheel (`pip wheel --no-build-isolation --no-deps`) en un directorio temporal con exactamente los archivos copiados por `Dockerfile` (resultado: `EXIT 0`).

5. **Launcher de Windows y test `test_claude_desktop_launcher_lists_tools` (`scripts/run_stdio.ps1`, `scripts/run_http.ps1`, `.venv`)**
   - **Qué se hizo:** Se diagnosticó una distribución corrupta residual (`~oogle-ads-mcp`) en `.venv/Lib/site-packages` que impedía que `google-ads-mcp.exe` resolviera `google_ads_server`. Se reinstaló el paquete en modo editable (`pip install --no-build-isolation --no-deps -e .`) y se endurecieron `scripts/run_stdio.ps1` y `scripts/run_http.ps1` añadiendo `$projectRoot` a `PYTHONPATH` cuando no está presente.
   - **Por qué:** En Windows, los wrappers `.exe` de entrypoints de consola no agregan el directorio actual al `sys.path` como sí hace `python script.py`; asegurar tanto el editable install como `PYTHONPATH` evita fallos silenciosos al arrancar desde Claude Desktop o scripts locales.

**Tests añadidos/actualizados:**
- [`tests/test_reports_bugs.py`](../tests/test_reports_bugs.py): 9 tests de regresión para `check_alerts`, consolidación multi-moneda y consistencia `pyproject.toml` ↔ `Dockerfile`.
- [`tests/test_consent_coverage.py`](../tests/test_consent_coverage.py): actualizado a las alertas y mensajes sin falsos positivos ni datos de cliente real.

### 2026-11-20 — Fase 0 · Tanda P0 (Limpieza de datos privados + verificación de disponibilidad en PyPI)

**Estado:** Limpieza de working tree/archivos trackeados completada (`490 passed` en `pytest`; `git grep` con 0 coincidencias de identificadores reales). Pendiente únicamente elegir el nombre definitivo del paquete en PyPI y revisar/limpiar historial Git previo antes de publicar el repositorio.

1. **Sanitización de configuración y plantillas (`config/clients.json`, `config/branding.json`, `config/clients.example.json`, `config/branding.example.json`, `.gitignore`)**
   - **Qué se hizo:** Se vació `config/clients.json` a `{}` y se restableció `config/branding.json` a valores genéricos (`Your Agency`). Se crearon `config/clients.example.json` y `config/branding.example.json` con datos ficticios (`1234567890`, `example.com`). Se agregó `tmp/` a `.gitignore`.
   - **Por qué:** Los archivos de configuración versionados contenían un cliente real, correo y dominio propios, que quedarían empaquetados tanto en Git como dentro de la imagen Docker (`COPY config ./config`).

2. **Retiro de artefacto privado del índice Git (`output/pdf/`)**
   - **Qué se hizo:** Se ejecutó `git rm --cached` sobre el PDF interno en `output/pdf/`, desvinculándolo de Git sin borrar la copia local en disco (el directorio `output/` ya estaba en `.gitignore`).
   - **Por qué:** Evita publicar documentos privados de diseño/cliente en el repositorio manteniendo intactos los archivos locales del autor. *Observación técnica:* antes de hacer público el repositorio conviene purgar ese objeto y commits previos con datos de clientes del historial Git (`git filter-repo` o repositorio limpio de publicación).

3. **Sanitización de documentación, scripts y tests (`docs/`, `scripts/resolve_ads.py`, 10 archivos en `tests/`)**
   - **Qué se hizo:** Se reemplazaron todos los IDs reales encontrados (cuenta cliente, MCC, campañas, presupuestos, ad groups, anuncios, conversion actions y nombres de cuenta) por placeholders determinísticos (`1234567890`, `9999999999`, `11111111111`, `Demo Account`, `output/demo`).
   - **Por qué:** Los ejemplos de documentación y fixtures de tests contenían IDs reales de producción que no deben exponerse en un paquete open source.

4. **Test automático de higiene del repositorio (`tests/test_hygiene.py`)**
   - **Qué se hizo:** Se añadieron 3 tests automáticos que verifican que `config/clients.json` permanezca vacío, `config/branding.json` sea genérico, las plantillas `*.example.json` usen dominios `example.com` y no haya archivos en `output/`, `data/`, `tmp/`, `generated/` ni `.env`/tokens trackeados en Git.
   - **Por qué:** Garantiza en cada ejecución de `pytest` (y luego en CI) que no vuelvan a colarse artefactos sensibles o de clientes.

5. **Verificación de nombre del paquete en PyPI**
   - **Qué se hizo:** Se consultó el índice público de PyPI (`https://pypi.org/pypi/<nombre>/json`).
   - **Resultado:** `google-ads-mcp` (`HTTP 200`) y `mcp-google-ads` (`HTTP 200`) **ya están ocupados**. Candidatos libres verificados (`HTTP 404`): `google-ads-mcp-server`, `gads-mcp`, `ads-mcp`, `ppc-google-ads-mcp`.

### 2026-11-20 — Fase 0 · Tanda P0 (Paquete `google-ads-mcp-server`, modo `--demo`, `doctor` y `connect` multi-cliente)

**Estado:** Completado (`497 passed` en `pytest`).

1. **Renombrado del paquete y CLI unificado (`pyproject.toml`, `google_ads_server.py`)**
   - **Qué se hizo:** Se renombró el paquete a `google-ads-mcp-server` (libre en PyPI y alineado con la convención estándar MCP), añadiendo el entrypoint `google-ads-mcp-server` mientras se conservan `google-ads-mcp` y `google-ads-mcp-connect` por retrocompatibilidad. Además, `google_ads_server.py:main()` delega los subcomandos `connect`, `register`, `doctor` y `serve` a `google_ads_connect.main()`.
   - **Por qué:** `uvx <paquete>` ejecuta el script con el mismo nombre del paquete; unificar el CLI bajo `google-ads-mcp-server` permite ejecutar `uvx google-ads-mcp-server --demo`, `uvx google-ads-mcp-server connect` y `uvx google-ads-mcp-server doctor` con un único comando.

2. **Modo `--demo` sin credenciales (`agency/demo.py`, `agency/api.py`, `agency/config.py`, `google_ads_server.py`)**
   - **Qué se hizo:** Se creó un dataset sintético determinístico con 2 cuentas en monedas y zonas horarias distintas (`1111111111` ecommerce en `EUR`/`Europe/Madrid` y `2222222222` B2B Leads en `USD`/`America/New_York`) cubriendo campañas Search/PMax, términos de búsqueda con gasto desperdiciado, keywords con Quality Score, conversiones primarias y secundarias, RSAs y alertas. En modo `--demo` se fuerza siempre `READ_ONLY_MODE=true` y `ALLOWED_CUSTOMER_IDS=1111111111,2222222222`, interceptando `agency.api.search()` y las rutas `googleAds:search` en `google_ads_server.py`.
   - **Por qué:** Permite a cualquier usuario probar el servidor completo (dashboard multi-moneda, waste, alertas, conversiones y guardarraíles) en menos de 1 minuto sin crear un proyecto en Google Cloud ni exponer cuentas reales.

3. **Preflight `doctor` y diagnóstico accionable de errores (`google_ads_connect.py`)**
   - **Qué se hizo:** Se añadió `diagnose_google_ads_error()` y `run_doctor()` (CLI `doctor`, `--offline`, `--json`) verificando versión de Python, perfil gestionado o `.env` (incluyendo colisión entre `GOOGLE_ADS_CREDENTIALS_PATH` y `GOOGLE_ADS_TOKEN_PATH`), guardarraíles, refresh OAuth, cuentas accesibles, metadatos por cuenta (`currency_code`, `time_zone`, `test_account`) y presencia de datos en los últimos 7 días.
   - **Por qué:** Diferencia claramente los 5 fallos frecuentes (`INVALID_CREDENTIALS`, `ACCESS_LEVEL_INSUFFICIENT`, `DEVELOPER_TOKEN_PROHIBITED`, `USER_PERMISSION_DENIED`, `ACCOUNT_NOT_FOUND` / `NO_DATA` en cuentas de prueba) con pasos concretos de resolución y sin exponer secretos.

4. **Asistente `connect` y `register` ampliados (`google_ads_connect.py`, `docs/connect.md`)**
   - **Qué se hizo:** Se añadieron los conectores `antigravity`, `gemini-cli` y `generic` (manteniendo `claude-desktop`, `claude-code`, `codex` y `cli`), soporte `--demo` en `register`, y soporte multi-cuenta (`--customer id1,id2`) + descubrimiento automático bajo MCC (`--manager <MCC> --all-mcc-clients`).
   - **Por qué:** Reduce la fricción de instalación en escritorio para todos los ecosistemas objetivo (Claude, Codex, Antigravity/Gemini y clientes genéricos como Cherry Studio/Cline/Cursor con DeepSeek o Kimi).

**Tests añadidos:**
- [`tests/test_demo_and_doctor.py`](../tests/test_demo_and_doctor.py): 7 tests (incluyendo E2E real sobre `stdio` MCP sin credenciales verificando `server_status`, `list_accounts`, `generate_agency_dashboard` multi-moneda, `detect_waste`, `check_alerts` y bloqueo de escritura; preflight `doctor` y registro en `antigravity`, `gemini-cli`, `generic` y `--all-mcc-clients`).

### 2026-11-20 — Cierre de Fase 0 (CI multiplataforma) + Fase 1 · Bloque 1 (Catálogo modular, perfiles Markdown+TOML, tope de filas y linter multi-IA)

**Estado:** Completado (`506 passed` en `pytest`).

1. **CI multiplataforma en GitHub Actions (`.github/workflows/ci.yml`)**
   - **Qué se hizo:** Se creó el pipeline con 3 jobs: `test` en matriz (`ubuntu-latest`, `macos-latest`, `windows-latest` × Python `3.11` y `3.12`) corriendo `pytest -q` y smoke test de `google-ads-mcp-server register --client generic --demo`; `package-and-docker` validando `pip wheel` y `docker build`; y `privacy-guard` bloqueando archivos de runtime/credenciales trackeados y patrones de claves/tokens OAuth.
   - **Por qué:** Cierra el criterio de salida multiplataforma de la Fase 0 y automatiza la protección contra regresiones de higiene y empaquetado en cada PR/push.

2. **Catálogo modular y ocultamiento de escrituras (`agency/modules.py`, `agency/registry.py`, `google_ads_server.py`)**
   - **Qué se hizo:** Se organizaron todas las tools en el módulo base `core` (**24 tools**, ≤25) más módulos optativos (`measurement`, `search`, `pmax`, `shopping`, `display`, `demand-gen`, `mcc`, `operations`) y presets de 1 palabra (`core`, `leads`, `ecommerce`, `agency`, `full`) configurables vía `--preset`, `--modules`, `GOOGLE_ADS_PRESET` o `GOOGLE_ADS_MODULES`. Cuando `READ_ONLY_MODE=true`, `configure_tool_catalog()` excluye por completo las 64 tools de `operations` de `tools/list` aunque se solicite `--preset full`. Se expuso `catalog` en `server_status()`.
   - **Por qué:** Reduce drásticamente el consumo de tokens y mejora la precisión de *tool calling* en OpenAI, Anthropic, Gemini, DeepSeek y Kimi, además de garantizar que los modelos no vean ni intenten invocar mutaciones en modo solo lectura.

3. **Perfiles de análisis personalizables Markdown + TOML (`agency/analysis_profiles.py`, `config/profiles/leads-audit.*`, `config/profiles/ecommerce-audit.*`)**
   - **Qué se hizo:** Se implementó un cargador de perfiles con precedencia `CONFIG_DIR/profiles` sobre `config/profiles` (plantilla `.md` + parámetros y umbrales `.toml` con `tomllib` stdlib, fusionando defaults de `alerts.json`). Se expusieron en doble vía: tools `list_analysis_profiles` y `get_analysis_profile` (con `reproducibility_header` y `security_notice`) + prompt MCP `analysis_profile_prompt`. Se añadieron dos perfiles iniciales listos para editar (`leads-audit` y `ecommerce-audit`) y validación anti path-traversal en `profile_id`.
   - **Por qué:** Permite a particulares y agencias estandarizar sus auditorías por vertical en clientes con soporte de prompts MCP y en clientes que solo ven herramientas, manteniendo intactas las reglas de seguridad del servidor.

4. **Protección de ventana de contexto y linter de JSON Schema multi-IA (`agency/api.py`, `agency/read_tools.py`, `agency/reports.py`, `tests/test_phase1_catalog_profiles.py`)**
   - **Qué se hizo:** Se añadió `search_with_meta()` con tope `max_rows` (por defecto `2000`, configurable vía `GOOGLE_ADS_MAX_SEARCH_ROWS`) e indicadores `returned_rows` / `truncated` en `agency/read_tools.py`. Se reemplazó el parámetro `branding: Optional[dict[str, Any]]` de `generate_client_report` por el modelo Pydantic tipado `ReportBranding` y se incorporó un linter automático de JSON Schema sobre `core` en `tests/test_phase1_catalog_profiles.py`.
   - **Por qué:** Evita desbordes de contexto cuando `googleAds:search` pagina decenas de miles de filas y garantiza que el 100% de las tools de `core` tenga esquemas portables sin objetos opacos que fallen en Gemini, DeepSeek o Kimi.

**Tests añadidos/actualizados:**
- [`tests/test_phase1_catalog_profiles.py`](../tests/test_phase1_catalog_profiles.py): 9 tests cubriendo catálogo modular, ocultamiento de `operations` en modo solo lectura, perfiles Markdown+TOML (incluyendo override de usuario y doble exposición), tope de filas `truncated` y linter multi-IA de JSON Schema.
- [`tests/test_guardrails.py`](../tests/test_guardrails.py) y [`tests/test_demo_and_doctor.py`](../tests/test_demo_and_doctor.py): actualizados para verificar explícitamente la activación/ocultamiento de `operations` según `READ_ONLY_MODE`.

### 2026-11-20 — Cierre de Fase 1 (Zona horaria, días cerrados, series temporales con *conversion lag*, multi-MCC dinámico y `RuntimeContext`)

**Estado:** Completado (`514 passed` en `pytest`).

1. **Zona horaria de la cuenta, días cerrados y *conversion lag* (`agency/dates.py`, `pyproject.toml`, `agency/reports.py`)**
   - **Qué se hizo:** Se creó `agency/dates.py` (`account_today`, `resolve_date_range`, `coverage_metadata`) usando `zoneinfo.ZoneInfo` con `tzdata>=2024.1; sys_platform == 'win32'` en `pyproject.toml` y fallback seguro a `UTC` si llega una zona desconocida. Por defecto `complete_days_only=True` cierra el rango en el día anterior (ayer) en la zona horaria de la cuenta, evitando mezclar horas incompletas del día en curso. Se integró en `_report_data()`, `generate_client_report()`, `generate_agency_dashboard()` y `compare_periods()`, adjuntando `currency_code`, `time_zone`, `queried_at_utc`, `complete_days_only`, `conversion_lag_warning` y `coverage_warnings`.
   - **Por qué:** Usar `date.today()` del equipo local producía discrepancias frente a la UI de Google Ads (especialmente en agencias con clientes en distintos husos horarios) e introducía sesgo al comparar el día en curso incompleto contra períodos cerrados. Además, advertir cuando el rango termina dentro de los últimos 3 días evita decisiones apresuradas sobre conversiones aún provisionales.

2. **Series temporales y fecha de conversión vs. interacción (`agency/reports.py`, `agency/demo.py`, `agency/modules.py`)**
   - **Qué se hizo:** Se incorporó la tool `get_performance_trends` al módulo `core` (dejando `core` exactamente en **25 tools**, dentro del tope ≤25) con `granularity="total" | "daily" | "weekly"`, filtro opcional por `campaign_id`, y soporte de `metrics.conversions_by_conversion_date` + `metrics.conversions_value_by_conversion_date` junto con `conversion_date_note`. Se usaron anotaciones `Annotated[..., Field(...)] = default` para compatibilidad tanto vía FastMCP como en llamadas Python directas.
   - **Por qué:** Permite detectar anomalías día a día, evaluar tendencias semanales y distinguir conversiones por fecha de clic (`conversions`) frente a fecha de conversión (`conversions_by_conversion_date`).

3. **Mapa dinámico multi-MCC y `RuntimeContext` / `StorageBackend` (`agency/runtime.py`, `agency/api.py`, `agency/mcc_tools.py`)**
   - **Qué se hizo:** Se introdujo `RuntimeContext` (gestionado por `ContextVar` con `use_runtime_context`) y el protocolo `StorageBackend` (implementación local por defecto `LocalFileStorageBackend`). Se añadió `resolve_login_customer_id()` con precedencia explícita (override por llamada → `RuntimeContext.login_customer_map` → mapa de sesión descubierto por `list_client_accounts()` → `GOOGLE_ADS_LOGIN_CUSTOMER_MAP` → `clients.json` → `GOOGLE_ADS_LOGIN_CUSTOMER_ID`), y `agency/api.py:_headers()` ahora elimina `login-customer-id` cuando una cuenta está marcada como directa (`""`).
   - **Por qué:** Habilita alternar cuentas directas y múltiples MCC dentro de una misma sesión sin reiniciar el servidor, y deja lista la frontera multi-tenant para una futura versión alojada (v1.1 / Cloud) sin reescribir las tools.

**Tests añadidos:**
- [`tests/test_phase1_runtime_dates.py`](../tests/test_phase1_runtime_dates.py): 8 tests validando rangos en `Europe/Madrid` vs. `America/New_York`, ventanas históricas sin alerta de lag, fallback de zona horaria, enrutamiento multi-MCC + cuentas directas sin header residual, aislamiento de `RuntimeContext` + `LocalFileStorageBackend`, `get_performance_trends` (diario y semanal) y `compare_periods`.

### 2026-11-20 — Fase 2 completada (Auditoría unificada de conversiones y diagnósticos de cargas offline)

**Estado:** Completado (`518 passed` en `pytest`).

1. **Auditoría unificada `audit_conversion_tracking` (`agency/measurement_tools.py`, `agency/modules.py`, `agency/registry.py`)**
   - **Qué se hizo:** Se implementó `audit_conversion_tracking` (en el módulo `measurement`, incluido en los presets `leads`, `ecommerce`, `agency` y `full`) junto al sumarizador puro `_summarize_conversion_audit()`. Cruza en una sola llamada: propiedad de conversiones (`google_ads_conversion_customer`, detectando centralización por MCC), `conversion_action`, `customer_conversion_goal`, `campaign_conversion_goal`, `custom_conversion_goal` + `campaign_custom_conversion_goal`, estrategias de puja y volumen/valor por acción. Emite alertas de alta señal (`MCC_CENTRALIZED_TRACKING`, `SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS`, `SMART_BIDDING_ZERO_CONVERSIONS`, `CUSTOM_GOAL_INCLUDES_SECONDARY_ACTION`, `MULTIPLE_PRIMARY_ACTIONS_SAME_CATEGORY`, `CUSTOMER_DATA_TERMS_NOT_ACCEPTED`, `EC4L_DISABLED_WITH_LEAD_ACTIONS` y `MEASUREMENT_SCOPE_DIFFERENCE` sin falsos positivos de Consent Mode).
   - **Por qué:** Sin cruzar objetivos de cuenta/campaña/personalizados con `primary_for_goal` y propietario MCC, un especialista no puede saber qué acciones guían realmente a Smart Bidding ni detectar doble conteo o acciones secundarias promovidas por objetivos personalizados.

2. **Diagnóstico de cargas offline y elegibilidad Data Manager API (`agency/measurement_tools.py`)**
   - **Qué se hizo:** Se implementó `get_offline_conversion_diagnostics` consultando `offline_conversion_upload_client_summary`, `offline_conversion_upload_conversion_action_summary` (con degradación elegante si una cuenta de prueba no soporta el recurso), acciones offline y configuración de Customer Data Terms. Devuelve conteos agregados (`sent_events`, `accepted_events`, `pending_events`, `failed_events`, `acceptance_rate`, `last_upload_date_time`) y alertas sin PII, distingue explícitamente ingestión API de atribución en campañas, y evalúa elegibilidad para migrar integraciones heredadas (`GOOGLE_ADS_API`, `UPLOAD_CLICKS`, `UPLOAD_CALLS`, `SALESFORCE`) a **Data Manager API**.
   - **Por qué:** Permite diagnosticar fallos de importación offline sin registrar datos personales y orientar la migración a Data Manager API antes de construir escritores nuevos.

3. **Integración en `--demo` y perfiles de análisis (`agency/demo.py`, `config/profiles/leads-audit.md`, `config/profiles/ecommerce-audit.md`)**
   - **Qué se hizo:** Se añadieron fixtures realistas de objetivos estándar, objetivo personalizado con acción offline secundaria (`2222222222`) y resúmenes de cargas offline al modo `--demo`, y se actualizaron las plantillas de auditoría `leads-audit.md` y `ecommerce-audit.md` para invocar ambas herramientas.
   - **Por qué:** Permite probar toda la Fase 2 en 30 segundos sin credenciales y estandariza su uso en las auditorías reproducibles.

**Tests añadidos:**
- [`tests/test_phase2_measurement.py`](../tests/test_phase2_measurement.py): 4 tests cubriendo objetivos estándar, personalizado con acción secundaria, propiedad centralizada por MCC, Smart Bidding sin acciones efectivas, riesgo de doble conteo, diagnósticos offline + elegibilidad Data Manager en `--demo` y linter multi-IA de JSON Schema sobre `core,measurement`.

### 2026-11-21 — Fase 3A completada (Diagnóstico 360°, pacing mensual, dimensiones faltantes y CLI de alertas)

**Estado:** Completado (`523 passed` en `pytest`).

1. **Diagnóstico unificado de entrega `diagnose_delivery` (`agency/diagnostics_tools.py`)**
   - **Qué se hizo:** Se implementó `diagnose_delivery` (y el sumarizador puro `_summarize_delivery_diagnosis`) cruzando estado de cuenta (`customer.status`), facturación (`billing_setup`), presupuesto de cuenta (`account_budget`), estados/razones de campaña (`primary_status`, `primary_status_reasons`, impression share perdido por presupuesto/ranking) y rechazos de políticas (`ad_group_ad.policy_summary`, `campaign_asset`/`asset.policy_summary`, `ad_group_criterion.approval_status`). Clasifica cada hallazgo explícitamente en `confirmed_causes`, `hypotheses` e `insufficient_data`.
   - **Por qué:** Facturación pendiente/agotada y anuncios o extensiones rechazados son causas frecuentes de caída súbita de impresiones que antes no tenían herramienta dedicada; separar causas confirmadas de hipótesis evita cambios a ciegas.

2. **Pacing de presupuesto mensual `get_budget_pacing` (`agency/diagnostics_tools.py`)**
   - **Qué se hizo:** Calcula el gasto acumulado del mes (MTD con días cerrados en la zona horaria de la cuenta), ritmo diario MTD y de los últimos 7 días, proyección a fin de mes, presupuesto restante, gasto diario necesario para llegar al objetivo y estado (`on_track`, `underspending`, `overspending`). Acepta `monthly_target_budget` explícito, `profile_id` TOML o infiere el objetivo desde presupuestos diarios activos.
   - **Por qué:** El control semanal de pacing es la consulta recurrente #1 de agencias y anunciantes.

3. **Landing pages, demografía, assets/calls y Quality Score desglosado (`agency/diagnostics_tools.py`, `agency/read_tools.py`)**
   - **Qué se hizo:** Se crearon `get_landing_page_performance` (`landing_page_view` con `mobile_friendly_clicks_percentage` y `speed_score`), `get_demographics_performance` (`age`, `gender`, `income`, `parental_status` o `all`) y `get_assets_and_calls_performance` (`campaign_asset` + `call_view` agregado sin PII). En `get_keyword_performance` se agregaron los 3 subcomponentes de Quality Score (`creative_quality_score`, `post_click_quality_score`, `search_predicted_ctr`). Todas se registraron en el nuevo módulo `diagnostics` (incluido automáticamente en los presets `leads`, `ecommerce`, `agency` y `full`, manteniendo `core` en 25 tools).
   - **Por qué:** Cierra las brechas de análisis semanal más habituales en Search y cuentas de leads/ecommerce sin obligar a escribir GAQL manual.

4. **CLI ejecutable de alertas (`agency/cli_alerts.py`, `google_ads_server.py`)**
   - **Qué se hizo:** Se añadió el subcomando `google-ads-mcp-server alerts` con `--customer`, `--profile <id>` (fusionando umbrales TOML del perfil sobre `alerts.json`), `--format markdown|json`, `--demo` y `--fail-on-alerts` (código de salida `2` si hay alertas disparadas).
   - **Por qué:** Permite ejecutar alertas programadas desde cron, GitHub Actions o scripts locales y enviar reportes Markdown/JSON a Slack/email sin infraestructura adicional.

**Tests añadidos:**
- [`tests/test_phase3a_diagnostics.py`](../tests/test_phase3a_diagnostics.py): 5 tests cubriendo separación `confirmed_causes`/`hypotheses`/`insufficient_data`, matemáticas de pacing mensual, ejecución completa de las 5 herramientas en `--demo`, CLI `alerts` en Markdown/JSON con perfil TOML y linter multi-IA de JSON Schema sobre `core,diagnostics`.

### 2026-11-21 — Fase 3B completada (Cobertura de canales: PMax, Shopping, Display/Demand Gen + Auction Insights CSV)

**Estado:** Completado (`526 passed` en `pytest`).

1. **Diagnóstico completo de Performance Max `get_pmax_diagnostics` (`agency/channels_tools.py`, módulo `pmax`)**
   - **Qué se hizo:** Cruza `asset_group`, `asset_group_asset` (performance labels `BEST/GOOD/LOW` + aprobaciones de política), `asset_group_signal` (audiencias y *search themes*), `asset_group_listing_group_filter`, `campaign_search_term_insight` y `performance_max_placement_view`. Emite alertas accionables (`PMAX_POOR_AD_STRENGTH`, `PMAX_DISAPPROVED_ASSETS`, `PMAX_LOW_PERFORMING_ASSETS`, `PMAX_ASSET_GROUP_ZERO_CONVERSIONS_SPEND`, `PMAX_HIGH_MOBILE_APP_IMPRESSIONS`).
   - **Por qué:** Elimina el punto ciego total que existía para auditar campañas Performance Max (solo había tools de creación).

2. **Rendimiento Shopping `get_shopping_performance` (`agency/channels_tools.py`, módulo `shopping`)**
   - **Qué se hizo:** Agrupa por `product`, `brand`, `category` (`shopping_performance_view`) o `product_group` (`product_group_view`), calculando ROAS, CPA y separando explícitamente `top_winners` de `wasted_products` (gasto sin conversiones por encima de `min_waste_spend`) con porcentaje de desperdicio sobre el gasto total.
   - **Por qué:** Permite a cualquier ecommerce detectar SKUs o categorías que queman presupuesto sin vender.

3. **Placements y Video/Demand Gen `get_placements_and_video_performance` (`agency/channels_tools.py`, módulos `display`, `demand-gen`)**
   - **Qué se hizo:** Consulta `detail_placement_view` detectando candidatos de exclusión (`MOBILE_APPLICATION` o gasto sin conversiones) y agrega métricas de video por campaña (`video_views`, `video_view_rate`, `average_cpv` y embudo de cuartiles `p25/p50/p75/p100`).
   - **Por qué:** Cubre *brand safety* y retención creativa en Display, Demand Gen y Video.

4. **Fallback Auction Insights por CSV `analyze_auction_insights_csv` (`agency/channels_tools.py`, módulos `search`, `pmax`, `shopping`)**
   - **Qué se hizo:** Parsea exportaciones CSV nativas de la UI de Google Ads en **inglés y español** (tolerando filas de título/rango previas, valores `< 10%`, `--` y decimales con coma o punto), preservando explícitamente `source: "google_ads_ui_csv_export"` y destacando competidores de alta presión.
   - **Por qué:** Resuelve de forma verificable la restricción de Google sobre las métricas `auction_insight_*` en API sin inventar datos.

5. **Plantillas de perfiles actualizadas y perfil de agencia (`config/profiles/agency-weekly.*`, `config/profiles/ecommerce-audit.md`, `agency/demo.py`)**
   - **Qué se hizo:** Se añadió el tercer perfil reproducible `agency-weekly` (TOML + Markdown), se actualizó `ecommerce-audit.md` con `get_pmax_diagnostics` y `get_shopping_performance`, y se agregaron fixtures realistas de PMax/Shopping/Placements a `--demo`.

**Tests añadidos:**
- [`tests/test_phase3b_channels.py`](../tests/test_phase3b_channels.py): 3 tests cubriendo PMax/Shopping/Placements en `--demo`, parsing de Auction Insights CSV en inglés y español, y el linter multi-IA de JSON Schema sobre **todos los módulos de lectura combinados** (`core,diagnostics,measurement,search,pmax,shopping,display,demand-gen`).

### 2026-11-21 — Fase 4 completada (Optimistic locking, escrituras prioritarias, reintento parcial en lotes y reversión ampliada)

**Estado:** Completado (`531 passed` en `pytest`).

1. **Verificación de estado obsoleto / *optimistic locking* (`agency/mutations.py`)**
   - **Qué se hizo:** Se añadió `verify_not_stale()` y los parámetros `state_verifier` + `stale_state_fields` en `execute_mutation()`. Antes de ejecutar cualquier mutación con `dry_run=False`, si el recurso en Google Ads cambió desde el snapshot tomado en `dry_run=True`, la aprobación se marca `stale`, se registra `stale_state` en el audit log y se aborta con `PermissionError` sin enviar la mutación.
   - **Por qué:** Evita sobrescribir cambios realizados por otro operador o desde la UI de Google Ads entre el preview y la confirmación.

2. **Escrituras prioritarias faltantes (`agency/operations_tools.py`, módulo `operations`)**
   - **Qué se hizo:** Se crearon 4 herramientas tipadas con `Annotated[..., Field(...)]`, confirmación exacta y `execute_mutation` (ocultas automáticamente en `READ_ONLY_MODE=true`):
     - `update_campaign_conversion_goal` (toggle `biddable` con *optimistic locking* y reversión).
     - `set_conversion_action_primary` (`primary_for_goal` con confirmación por nombre exacto, *optimistic locking* y reversión).
     - `exclude_placements` (exclusión de URLs o apps móviles a nivel `CAMPAIGN` o `ACCOUNT` con deduplicación y tope de 100).
     - `add_account_negative_keywords` (`customerNegativeCriteria` aplicable a Search, Shopping y PMax).
   - **Por qué:** Cierra las operaciones de alto impacto que faltaban en medición y *brand safety* bajo el mismo contrato de seguridad.

3. **Reintento parcial en lotes (`agency/mcc_tools.py`)**
   - **Qué se hizo:** `mcc_bulk_apply` ahora persiste `executed_customer_ids` y `failed_targets` en el registro de aprobación, expone `retryable_customer_ids` + `retry_next_step`, y acepta `retry_failed_from_approval_id` para previsualizar y reejecutar **exclusivamente** las cuentas fallidas sin duplicar las que ya se ejecutaron.
   - **Por qué:** En lotes multi-cliente, un fallo transitorio en una cuenta no debe obligar a reconstruir ni re-aplicar todo el lote.

4. **Reversión ampliada (`agency/governance_tools.py`)**
   - **Qué se hizo:** Se amplió `revert_operation` para soportar además `update_keyword_bid`, `pause_keyword`, `enable_keyword`, `set_conversion_action_primary` y `update_campaign_conversion_goal`.
   - **Por qué:** Reduce el riesgo operativo al habilitar escrituras en cuentas reales de agencia.

**Tests añadidos:**
- [`tests/test_phase4_operations.py`](../tests/test_phase4_operations.py): 5 tests cubriendo rechazo de aprobaciones obsoletas (*optimistic locking*), ejecución + reversión de `update_campaign_conversion_goal` y `set_conversion_action_primary`, validación de `exclude_placements` y `add_account_negative_keywords`, reintento parcial en `mcc_bulk_apply` sin duplicar cuentas exitosas, y ocultamiento/linter de esquemas en modo lectura vs. escritura.

### 2026-11-21 — Fase 5 completada (Documentación pública EN/ES, matriz de compatibilidad y checklist de release v1.0)

**Estado:** Base técnica, pruebas (`531 passed`), CI multiplataforma, empaquetado wheel y documentación pública v1.0 **completados**. Solo restan los pasos externos de publicación humana (pilotos en cuentas reales, purga del historial Git previo y publicación en PyPI/GHCR/registros MCP), todos listados en `docs/release-checklist.md`.

1. **Documentación principal global en inglés + guía rápida en español (`README.md`, `docs/es/README.md`, `docs/connect.md`)**
   - **Qué se hizo:** Se reescribió `README.md` en inglés con quickstart de 30s (`--demo`), flujo `connect` + `doctor`, tabla de presets/módulos, perfiles Markdown+TOML, CLI `alerts`, advertencias claras sobre `Streamable HTTP` autogestionado y enlace directo a `docs/es/README.md` (con quickstart y prompts listos por rol en español).
   - **Por qué:** Facilita la adopción internacional en GitHub, PyPI y directorios MCP sin perder documentación de primer nivel en español.

2. **Matriz pública de compatibilidad (`docs/compatibility-matrix.md`)**
   - **Qué se hizo:** Se publicó la matriz completa y verificable por aplicación/ecosistema de IA (Claude Desktop/`.mcpb`, Claude Code, Codex, Antigravity, Gemini CLI, clientes de escritorio con DeepSeek y Kimi + `generic` y estado v1.1 de apps web/móviles), por módulo/preset (`core` 25 tools + 9 módulos optativos), por cobertura de canal y por limitaciones oficiales de Google Ads API (Auction Insights CSV, Consent Mode, cuentas de prueba y Data Manager API).
   - **Por qué:** Alinea expectativas de forma transparente con usuarios y agencias antes de instalar.

3. **Arquitectura actualizada, `.env.example` y checklist de publicación (`DESIGN.md`, `.env.example`, `docs/release-checklist.md`)**
   - **Qué se hizo:** Se actualizó `DESIGN.md` reflejando todos los nuevos componentes e invariantes; se añadieron a `.env.example` las variables `GOOGLE_ADS_LOGIN_CUSTOMER_MAP`, `GOOGLE_ADS_PRESET`, `GOOGLE_ADS_MODULES` y `GOOGLE_ADS_MAX_SEARCH_ROWS`; y se creó `docs/release-checklist.md` con los criterios de salida de seguridad/historial Git, CI, 3 pilotos reales, publicación PyPI/GHCR/MCP Registry y política SemVer/API sunset.
   - **Por qué:** Cierra el ciclo de ingeniería v1.0 dejando un procedimiento repetible y sin cabos sueltos para el lanzamiento público.

### 2026-11-21 — Correcciones de auditoría post-v2 (empaquetado wheel, presets en `connect`/`serve`, compatibilidad real Google Ads API v25, seguridad HTTP, truncamiento y *optimistic locking* ampliado)

**Estado:** Todos los hallazgos técnicos de código (P0 y P1) de la auditoría de implementación quedan resueltos y cubiertos por pruebas automatizadas (**`542 passed`** en `pytest`). Siguen pendientes exclusivamente pasos externos/humanos de publicación: purga del historial Git anterior, validación en apps de escritorio reales, 3 pilotos en cuentas reales, artefacto `.mcpb` (planificado) y publicación en PyPI/GHCR/MCP Registry.

1. **Perfiles embebidos en el wheel fuera del repo (`agency/builtin_profiles/`, `pyproject.toml`, `agency/analysis_profiles.py`)**
   - **Qué se hizo:** Se incluyeron los perfiles built-in (`leads-audit`, `ecommerce-audit`, `agency-weekly` en `.toml` y `.md`) dentro del paquete `agency/builtin_profiles/` mediante `[tool.setuptools.package-data]` en `pyproject.toml`, y `agency/analysis_profiles.py` ahora resuelve por orden `GOOGLE_ADS_CONFIG_DIR/profiles` (override de usuario) → `config/profiles` (checkout) → `importlib.resources.files("agency").joinpath("builtin_profiles")` (wheel instalado).
   - **Por qué:** Al instalar vía `pip`/`uvx` fuera del repositorio clonado, `PROJECT_ROOT / "config" / "profiles"` no existía y `list_analysis_profiles()` devolvía vacío. Validado con un test real que construye el `.whl`, lo descomprime en un directorio aislado fuera del repo y ejecuta Python sin acceso al checkout.

2. **Soporte `--preset` y `--modules` en `connect`, `register` y `serve` (`google_ads_connect.py`, `docs/connect.md`)**
   - **Qué se hizo:** Se añadieron `--preset` y `--modules` a `connect`, `register` y `serve`; `authorize()` persiste los valores elegidos en `profile.json`; `server_command()` y `demo_server_command()` propagan los flags al comando registrado en cada cliente; y `serve()` aplica `configure_tool_catalog(..., read_only=True)` antes de `mcp.run(transport="stdio")`, manteniendo ocultas el 100% de las tools de `operations` incluso con `--preset full`.
   - **Por qué:** Sin estos flags en el flujo principal `connect`/`register`/`serve`, los usuarios de escritorio quedaban bloqueados en `core` (25 tools) sin poder activar `leads`, `ecommerce` o `agency` en perfiles gestionados.

3. **Correcciones reales de GAQL para Google Ads API v25 (`agency/measurement_tools.py`, `agency/channels_tools.py`, `agency/demo.py`)**
   - **Qué se hizo:**
     - `audit_conversion_tracking`: se reemplazó `FROM campaign_custom_conversion_goal` (inválido en v25: `BAD_RESOURCE_TYPE_IN_FROM_CLAUSE`) por `FROM conversion_goal_campaign_config` seleccionando `campaign.id`, `conversion_goal_campaign_config.goal_config_level` y `conversion_goal_campaign_config.custom_conversion_goal`, respetando `goalConfigLevel == "CUSTOMER"` y añadiendo `coverage_complete: bool` en ambas tools de medición.
     - `get_pmax_diagnostics`: se eliminó `asset_group_asset.performance_label` (`UNRECOGNIZED_FIELD` en v25) reemplazándolo por `asset_group_asset.primary_status`, `asset_group_asset.primary_status_reasons` y `asset_group_asset.policy_summary.approval_status` (alerta `PMAX_LIMITED_OR_INELIGIBLE_ASSETS`); y se corrigió `campaign_search_term_insight` para consultar campaña por campaña con filtro único `campaign_search_term_insight.campaign_id = <id>` (top 10 campañas PMax por gasto), además de exponer `coverage_complete: bool`.
   - **Por qué:** Detectado en la auditoría contra el esquema real de Google Ads API v25; evita fallos silenciosos que dejaban incompletos los diagnósticos de objetivos personalizados y PMax.

4. **`.dockerignore`, seguridad HTTP por defecto y launchers Windows CI (`Dockerfile`, `.dockerignore`, `google_ads_server.py`, `scripts/run_stdio.ps1`, `scripts/run_http.ps1`)**
   - **Qué se hizo:** Se corrigió `.dockerignore` (retirando `*.json` y `docs` globales para no romper `COPY config/ docs/` en `Dockerfile`, ignorando solo JSONs sensibles y carpetas de runtime/tests/tmp); se cambió el default de `Dockerfile` a `MCP_HOST=127.0.0.1`; `google_ads_server.py` ahora rechaza iniciar `streamable-http` en hosts no-loopback (`0.0.0.0`, etc.) salvo opt-in explícito `MCP_HTTP_ALLOW_REMOTE=true`; y los scripts `.ps1` hacen fallback a `google-ads-mcp-server` en `PATH` cuando no existe `.venv` (arreglando CI en Windows).
   - **Por qué:** Cierra fallos de build Docker, evita exposición accidental en red local sin autenticación y garantiza portabilidad en CI Windows sin `.venv`.

5. **Zona horaria en `detect_waste`/`generate_optimization_plan`, señalización de truncamiento y *optimistic locking* ampliado (`agency/reports.py`, `agency/channels_tools.py`, `agency/write_tools.py`, `docs/compatibility-matrix.md`)**
   - **Qué se hizo:** `detect_waste` y `generate_optimization_plan` migraron a `_account_metadata` + `resolve_date_range(complete_days_only=True)` + `coverage_metadata`; `_report_data`, `get_performance_trends`, `detect_waste`, `get_shopping_performance` y `get_placements_and_video_performance` exponen `truncated` + `totals_are_partial` (y `truncation_warning` al alcanzar `GOOGLE_ADS_MAX_SEARCH_ROWS`); se añadió `state_verifier` (*optimistic locking*) a las escrituras de alto impacto en `agency/write_tools.py` (`update_campaign_budget`, `pause_campaign`, `enable_campaign`, `remove_campaign`, `update_keyword_bid`); y se ajustó `docs/compatibility-matrix.md` para marcar `.mcpb` como planificado y documentar los estados reales de verificación.
   - **Por qué:** Evita totales parciales silenciosos por truncamiento, elimina inconsistencias de zona horaria entre tools de reporte y protege presupuestos/estados/pujas contra aprobaciones obsoletas.

**Tests añadidos:**
- [`tests/test_audit_fixes.py`](../tests/test_audit_fixes.py): 11 tests de regresión cubriendo construcción y extracción real del wheel fuera del repo (con override de usuario), E2E `stdio` de `connect.py serve --preset full` validando exposición de módulos y cero fugas de escritura, rechazo de HTTP no-loopback sin opt-in, consistencia `.dockerignore`/`Dockerfile`, fallback de launchers `.ps1`, contratos GAQL v25 (`conversion_goal_campaign_config`, `asset_group_asset.primary_status`, `campaign_search_term_insight.campaign_id`), zona horaria/truncamiento (`totals_are_partial`) y rechazo de aprobaciones obsoletas en `update_campaign_budget` y `pause_campaign`.

### 2026-11-21 — Cierre de hallazgos de código del Roadmap v3 (dispatcher `google_ads_cli.py`, cobertura explícita de muestreo PMax, propagación total de truncamiento y matriz de escrituras por familia)

**Estado:** Todos los hallazgos técnicos de código del [Roadmap v3](roadmap-marketing-v3.md) (2 P0 + P1 de truncamiento y garantías por familia) resueltos y verificados (**`546 passed in 27.86s`** en `pytest`).

1. **Arranque directo con perfil gestionado (`google_ads_cli.py`, `pyproject.toml`, `Dockerfile`, `google_ads_server.py`, `google_ads_connect.py`, `agency/analysis_profiles.py`)**
   - **Qué se hizo:** Se creó el dispatcher liviano `google_ads_cli.py` como entrypoint de `google-ads-mcp-server` y `google-ads-mcp` (empaquetado en `py-modules` y copiado en `Dockerfile`), ruteando `connect`, `register`, `doctor`, `serve` (incluso precedidos de `--profile <ruta>`) y `alerts` **antes** de importar `google_ads_server` o `agency`. Como segunda barrera defensiva (por ejemplo al invocar `python google_ads_server.py --profile ... serve`), se añadió `google_ads_server.apply_managed_profile()` para refrescar `MANAGED_PROFILE`, `MANAGED_CREDENTIALS`, credenciales/headers y rutas `CONFIG_DIR`/`DATA_DIR` de `agency`.
   - **Por qué:** Antes, `google-ads-mcp-server serve` importaba `google_ads_server` antes de que `serve()` poblara `os.environ`, cargando `.env` y dejando `MANAGED_PROFILE = None`.

2. **Transparencia de cobertura y muestreo en Performance Max (`agency/channels_tools.py`)**
   - **Qué se hizo:** `get_pmax_diagnostics` expone ahora `coverage_status` (`complete` | `empty` | `sampled` | `partial_error`), `search_term_insights_coverage` (`eligible_campaigns_count`, `queried_campaigns_count`, `omitted_campaigns_count`, `campaign_limit`, `per_campaign_limit`, `global_limit`, `capped_by_*`, `combined_rows_before_global_cap`, `returned_rows`, `failed_campaigns_count`, `status`), `placements_coverage` y `sampling_warnings`. `coverage_complete` pasa a `False` siempre que exista error o muestreo por límite.
   - **Por qué:** Evita marcar `coverage_complete: true` cuando hay más de 10 campañas PMax o cuando los límites por campaña (25) o globales (50/100) recortan el universo analizado.

3. **Propagación completa de truncamiento y matriz de escrituras por familia (`agency/diagnostics_tools.py`, `agency/reports.py`, `agency/measurement_tools.py`, `docs/compatibility-matrix.md`, `docs/roadmap-marketing-v3.md`)**
   - **Qué se hizo:** Se propagaron `truncated`, `totals_are_partial` y `truncation_warning` en `get_budget_pacing`, `diagnose_delivery`, `get_assets_and_calls_performance`, `generate_agency_dashboard` (con `truncated_customer_ids`), `compare_periods` y `audit_conversion_tracking`. Se agregó la Sección 5 (*Write Safety Guarantees by Family*) en `docs/compatibility-matrix.md` detallando las garantías exactas de cada familia de escritura (`write_tools`, `operations_tools`, `experiment_tools`, `demand_gen_tools`, `customer_match_tools`, `offline_conversions_tools`, `mcc_tools`).
   - **Por qué:** Cierra los últimos agregadores que podían sumar métricas parciales sin aviso explícito y deja documentado con honestidad el alcance exacto de `state_verifier`, `validateOnly` y `revert_operation` por familia.

**Tests añadidos:**
- [`tests/test_audit_fixes.py`](../tests/test_audit_fixes.py) ampliado a **15 pruebas**: añade E2E `stdio` parametrizado para `google_ads_cli.py` y `google_ads_server.py` con `--profile ... serve` y variables `.env` señuelo (`READ_ONLY_MODE=false`, dev token falso) verificando `server_status` y catálogo `ecommerce` sin fugas de escritura; 4 escenarios de cobertura PMax (`empty`, `sampled` por 12 campañas, `sampled` por 25/50 filas y `partial_error`); y propagación de truncamiento en `get_budget_pacing`, `diagnose_delivery`, `generate_agency_dashboard` y `audit_conversion_tracking`.

### 2026-11-21 — Cierre de truncamiento paginado en subconsultas de PMax, auditoría de conversiones y diagnóstico de entrega

**Estado:** Completado y verificado con respuestas paginadas (`nextPageToken`) en **`548 passed in 30.13s`** (`tests/test_audit_fixes.py` ampliado a 17 pruebas).

1. **P0 · Truncamiento paginado en todas las subconsultas de `get_pmax_diagnostics` (`agency/channels_tools.py`)**
   - **Qué se hizo:** Se reemplazó `search()` / `_safe_search()` por `_search_bounded()` y `_safe_search_bounded()` en las 6 subconsultas de `get_pmax_diagnostics` (`asset_group`, `asset_group_asset`, `asset_group_signal`, `asset_group_listing_group_filter`, `campaign_search_term_insight` y `performance_max_placement_view`), agregando `truncated`, `totals_are_partial`, `truncated_queries`, `truncation_warning` y `capped_by_api_row_limit`. Si cualquier subconsulta se corta por `GOOGLE_ADS_MAX_SEARCH_ROWS`, `coverage_complete` pasa a `False`, `coverage_status` a `"sampled"` (o `"partial_error"`) y se añade advertencia explícita en `sampling_warnings`.
   - **Por qué:** Una respuesta paginada truncada en `asset_group` podía devolver `coverage_complete: true` sin advertencias aunque faltaran grupos de recursos y campañas PMax completas.

2. **P1 · Truncamiento en consultas de configuración de medición y entrega (`agency/measurement_tools.py`, `agency/diagnostics_tools.py`)**
   - **Qué se hizo:** En `audit_conversion_tracking` (`conversion_action`, `customer_conversion_goal`, `campaign_conversion_goal`, `custom_conversion_goal`, `conversion_goal_campaign_config`, además de métricas), `get_offline_conversion_diagnostics` (`conversion_action`, `offline_conversion_upload_client_summary`, `offline_conversion_upload_conversion_action_summary`) y `diagnose_delivery` (`billing_setup`, `account_budget`, `campaign`, `ad_group_ad`, `campaign_asset`, `keyword_view`), todas las subconsultas ahora reportan `truncated`, `truncated_queries`, `truncation_warning` y marcan `coverage_complete: false` ante cualquier recorte.
   - **Por qué:** Si se truncaba `conversion_action` (consulta de configuración) mientras las métricas no se truncaban, el auditor reportaba `truncated: false` y `coverage_complete: true` omitiendo acciones reales de la cuenta.

**Tests añadidos:**
- `test_pmax_paginated_asset_group_truncation_marks_coverage_incomplete` y `test_conversion_audit_config_query_truncation_marks_coverage_incomplete` en [`tests/test_audit_fixes.py`](../tests/test_audit_fixes.py) reproduciendo respuestas paginadas con `nextPageToken` vía `agency.api.request`.

### 2026-11-21 — Exactitud diagnóstica ante datos parciales (degradación a `insufficient_data` de alertas basadas en ausencia)

**Estado:** Completado y verificado (**`550 passed in 34.21s`**; `tests/test_audit_fixes.py` ampliado a 19 pruebas).

1. **Degradación de alertas basadas en ausencia en medición (`agency/measurement_tools.py`)**
   - **Qué se hizo:** En `_summarize_conversion_audit()` y `_summarize_offline_diagnostics()` se agregó `insufficient_data` y se bloqueó la emisión de alertas basadas en ausencia cuando su consulta de respaldo fue truncada o una consulta opcional falló: `SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS` se degrada a `INSUFFICIENT_DATA_EFFECTIVE_ACTIONS` si se trunca `conversion_action` o cualquier consulta de objetivos, o si falla alguna consulta opcional de objetivos (`customer_conversion_goal`, `campaign_conversion_goal`, `custom_conversion_goal`, `conversion_goal_campaign_config`; un fallo API en la consulta principal `conversion_action` sigue propagando `GoogleAdsError` de forma intencional); `PRIMARY_ACTION_NO_VOLUME` se degrada a `INSUFFICIENT_DATA_ACTION_VOLUME` si se trunca `campaign_conversion_action_metrics`; y `NO_RECENT_OFFLINE_UPLOADS` se degrada a `INSUFFICIENT_DATA_OFFLINE_UPLOADS` si los resúmenes opcionales offline fueron truncados o fallaron.
   - **Por qué:** Evita falsos positivos críticos afirmando que una campaña Smart Bidding no tiene acciones efectivas cuando una acción válida podría estar en una página omitida por truncamiento.

2. **Degradación de causa confirmada de facturación en entrega (`agency/diagnostics_tools.py`) y simetría de contrato PMax (`agency/channels_tools.py`)**
   - **Qué se hizo:** En `_summarize_delivery_diagnosis()`, si `billing_setup` fue truncada y las filas visibles no están `APPROVED`, ya no se emite `NO_APPROVED_BILLING_SETUP` en `confirmed_causes`: se degrada a `BILLING_SETUP_TRUNCATED` en `insufficient_data` (y se registra `DIAGNOSTIC_QUERY_TRUNCATED` para cualquier otra consulta truncada). En `get_pmax_diagnostics`, se añadió `placements_coverage.capped_by_api_row_limit` para simetría de contrato junto a `truncated_queries` (6 subconsultas) y `search_term_insights_coverage.capped_by_api_row_limit`.
   - **Por qué:** Garantiza que `confirmed_causes` contenga exclusivamente causas confirmadas por evidencia positiva o ausencia verificada sobre datos completos.

**Tests añadidos:**
- `test_truncated_conversion_actions_suppresses_smart_bidding_without_effective_actions` y `test_truncated_billing_setup_suppresses_no_approved_billing_confirmed_cause` en [`tests/test_audit_fixes.py`](../tests/test_audit_fixes.py) (cada uno validando tanto el caso truncado degradado a `insufficient_data` como el caso control completo).

### 2026-11-21 — Hosted Beta Self-Serve (Opción 2: onboarding web OAuth + cifrado Fernet + pool dinámico de workers + presets remotos)

**Estado:** Implementado y verificado (**`560 passed in 36.46s`**; 10 pruebas remotas en `test_remote_gateway.py` + `test_remote_self_serve.py`).

1. **Almacén cifrado de tenants y pool dinámico bajo demanda (`agency/remote_store.py`)**
   - **Qué se hizo:** Se implementó `EncryptedProfileStore` sobre SQLite + cifrado Fernet (`MCP_REMOTE_MASTER_KEY`), garantizando que ningún `refresh_token` se guarde en texto plano ni en `tenants.db` ni en el `profile.json` efímero de cada worker (`format: "fernet-v1"`, descifrado solo en memoria por `google_ads_connect.serve()`). Se creó `DynamicWorkerPool` con spawn perezoso por `subject` (lock por usuario para evitar duplicados concurrentes), puertos loopback efímeros, evicción por inactividad (`idle_timeout_seconds`), tope LRU (`max_active_workers`) y parada + borrado inmediato al reconfigurar o revocar.
   - **Por qué:** Elimina la necesidad de provisionar puertos fijos y reiniciar el gateway por cada usuario nuevo, acota la RAM en VPS y evita credenciales en texto plano en disco.

2. **Portal web self-serve OAuth 2.0 + PKCE (`agency/remote_onboarding.py`, `agency/remote_gateway.py`)**
   - **Qué se hizo:** Se añadieron las rutas `/connect`, `/connect/session` (cookie `HttpOnly; Secure; SameSite=Lax` firmada con Fernet), `/connect/status`, `/connect/google/start` (PKCE `S256` + `state` CSRF ligado al `sub`), `/connect/google/callback` (descubrimiento de cuentas directas y subcuentas MCC), `/connect/configure` (validando que las cuentas pertenezcan al grant y bloqueando escrituras salvo `MCP_REMOTE_ALLOW_USER_WRITES=true`) y `/connect/disconnect` (revocación en Google + borrado en SQLite + parada del worker). Cuando un usuario autenticado llama a `/mcp` sin conectar Google Ads, el gateway responde `403` con `connect_url`.
   - **Por qué:** Permite que cualquier usuario externo conecte, elija cuentas/preset y revoque su Google Ads desde el navegador sin intervención manual del operador.

3. **Respeto de presets en workers remotos y CSV inline (`google_ads_connect.py`, `agency/channels_tools.py`, `pyproject.toml`, `docs/remote-pilot.md`)**
   - **Qué se hizo:** `serve(--worker-port)` ahora respeta `--preset`/`--modules` y el preset guardado en el perfil (solo usa `full` por defecto cuando se pasa `--allow-writes` sin preset explícito); `analyze_auction_insights_csv` acepta `csv_content` inline (hasta 2 MB) para usuarios remotos sin acceso al disco del host; se agregó `cryptography>=42.0` a `[project.optional-dependencies].remote`; y se documentó el despliegue self-serve y el checklist de verificación OAuth de Google en `docs/remote-pilot.md`.
   - **Por qué:** Evita exponer las 142 tools innecesariamente en sesiones remotas y habilita el fallback de Auction Insights también desde clientes remotos.

**Tests añadidos:**
- [`tests/test_remote_self_serve.py`](../tests/test_remote_self_serve.py): 5 pruebas cubriendo ausencia de tokens en claro en SQLite y disco, ciclo de vida del `DynamicWorkerPool` (concurrencia, LRU, idle timeout, cambio de preset), flujo completo `/connect` → PKCE/CSRF (bloqueando replay cross-user, cuentas no autorizadas y escrituras deshabilitadas) → `/mcp` → `/connect/disconnect`, worker HTTP real en subproceso con perfil Fernet cifrado y preset `ecommerce` respetado (401 sin clave, 0 tools de escritura), y `analyze_auction_insights_csv` con `csv_content` inline.

### 2026-11-21 — Hardening de Hosted Beta Self-Serve (login web sin Bearer copy-paste, rutas MCC por cuenta, reserva atómica de pool e in-flight draining, alineación de escrituras y catálogo)

**Estado:** Los 4 hallazgos de auditoría sobre el modo self-serve quedaron resueltos y probados (**`562 passed in 35.25s`**; 12 pruebas remotas en `test_remote_gateway.py` + `test_remote_self_serve.py`).

1. **Inicio de sesión web sin pedir Bearer token (`agency/remote_gateway.py`, `agency/remote_onboarding.py`)**
   - **Qué se hizo:** Cuando `/mcp` detecta un usuario autenticado aún no conectado a Google Ads, emite un ticket de un solo uso (10 min, cifrado en `oauth_pending`) y devuelve `connect_url = https://<host>/connect/ticket?t=<ticket>`, que al abrirse en navegador crea automáticamente la cookie `HttpOnly` para ese mismo `sub`. Además se añadió login web OIDC/OAuth + PKCE (`GET /connect/login` + `GET /connect/oidc/callback` con `MCP_REMOTE_PORTAL_CLIENT_ID`) verificado con el mismo `OAuthVerifier`.
   - **Por qué:** Abrir un enlace desde Claude Desktop o Codex no transfiere el header `Authorization` al navegador; ambos mecanismos eliminan cualquier copy-paste manual de tokens.

2. **Rutas de acceso por cuenta (`customer_routes`) y bloqueo de edición no validada de MCC (`agency/remote_store.py`, `agency/remote_onboarding.py`, `google_ads_connect.py`, `google_ads_server.py`)**
   - **Qué se hizo:** `tenants.db` guarda ahora `customer_routes_json` (`cid -> "" | mcc_id`), validado par a par contra el grant de Google Ads; editar preferencias sin reautorizar rechaza cualquier cuenta o `manager` nuevo (`403 reconnect_required_for_route_change`); `serve()` traduce `customer_routes` a `GOOGLE_ADS_LOGIN_CUSTOMER_MAP` (sin `GOOGLE_ADS_LOGIN_CUSTOMER_ID` global cuando hay rutas mixtas); y `google_ads_server.py` aplica `_apply_per_customer_login_header()` en `http_get`/`http_post` para todas las tools core.
   - **Por qué:** Permite mezclar cuentas directas con múltiples MCC en el mismo usuario sin fugas de `login-customer-id` y cierra la escalada de permisos al editar `manager`.

3. **Reserva atómica de capacidad y protección de solicitudes en vuelo (`agency/remote_store.py`, `agency/remote_gateway.py`)**
   - **Qué se hizo:** `DynamicWorkerPool` reserva plaza en `_reserved_subjects` bajo `_pool_lock` **antes** de spawnear (impidiendo exceder `max_active_workers` bajo altas simultáneas) y rastrea `in_flight` vía `acquire_worker_handle` / `release_worker_handle` durante toda la vida del `StreamingResponse`. La evicción idle/LRU nunca termina workers con `in_flight > 0` (si todo está ocupado responde `HTTP 503 Retry-After: 2`), y `stop_worker` marca `draining=True` esperando drenaje antes de matar el proceso.
   - **Por qué:** Garantiza el tope de RAM bajo concurrencia y evita cortar escrituras/consultas activas en curso.

4. **Alineación entre `allow_writes` y el catálogo de tools (`agency/remote_onboarding.py`, `google_ads_connect.py`)**
   - **Qué se hizo:** Cuando `allow_writes=True`, si no se especifica preset se usa `full`; si se elige otro preset (`core`, `leads`, `ecommerce`, `agency`), se incluye automáticamente el módulo `operations`. El portal HTML preselecciona `full` cuando `MCP_REMOTE_ALLOW_USER_WRITES=true` y describe las tools exactas de cada preset.
   - **Por qué:** Evita guardar `allow_writes=True` con un catálogo que no expone ninguna tool de escritura.

**Tests añadidos:**
- `test_portal_oidc_login_mixed_mcc_routes_and_write_preset_alignment` y `test_worker_pool_strict_capacity_under_concurrency_and_inflight_protection` en [`tests/test_remote_self_serve.py`](../tests/test_remote_self_serve.py) (reproduciendo exactamente las 2 altas concurrentes con `max_active_workers=1`, protección `in_flight`, ticket 1-clic + OIDC, cuentas directas + 2 MCCs distintos, rechazo `403` de edición de manager y auto-inclusión de `operations`).

### 2026-11-21 — Cierre de seguridad RFC 9700, precedencia de rutas en `runtime.py`, draining estricto y política global de escrituras en runtime

**Estado:** Completado y verificado (**`564 passed in 41.16s`**; 14 pruebas remotas en `test_remote_gateway.py` + `test_remote_self_serve.py`).

1. **Login OIDC ligado al navegador (RFC 9700) y protección de tickets en logs (`agency/remote_onboarding.py`, `agency/remote_gateway.py`, `docs/remote-pilot.md`)**
   - **Qué se hizo:** `GET /connect/login` emite la cookie `HttpOnly; Secure; SameSite=Lax` cifrada `mcp_oidc_tx` (`state` + `tx_nonce`, `Path=/connect/oidc/callback`) y `GET /connect/oidc/callback` rechaza cualquier callback de otro navegador (`400 oidc_browser_mismatch`) sin consumir el `state` legítimo. Además `/connect/ticket` y `/connect/oidc/callback` responden con `Cache-Control: no-store`, `Pragma: no-cache`, `Referrer-Policy: no-referrer`, y `remote_gateway.main()` desactiva `uvicorn` `access_log` para no registrar `?t=<ticket>` ni `?code=...` en logs (documentando el mismo requisito para el proxy HTTPS).

2. **Precedencia de rutas fijadas en `agency/runtime.py`**
   - **Qué se hizo:** En `resolve_login_customer_id()`, `GOOGLE_ADS_LOGIN_CUSTOMER_MAP` (rutas autorizadas del perfil) ahora tiene precedencia **por encima** de `_session_login_map`, y `register_login_customer()` no sobrescribe cuentas ya fijadas en `GOOGLE_ADS_LOGIN_CUSTOMER_MAP`.
   - **Por qué:** Impide que `list_client_accounts()` sobrescriba en memoria una cuenta autorizada como directa con un MCC descubierto dinámicamente.

3. **Draining contabilizado e ininterrumpible + política global de escrituras en el pool (`agency/remote_store.py`, `agency/remote_gateway.py`, `agency/remote_onboarding.py`)**
   - **Qué se hizo:** `DynamicWorkerPool` mantiene los workers en drenaje dentro de `_draining_workers` (contabilizados en `active_worker_count()` y en `max_active_workers`), nunca mata un worker con `in_flight > 0` al vencer `drain_timeout_seconds` en `stop_worker` (lo termina automáticamente dentro de `release_worker_handle` en cuanto concluye la última solicitud en curso), y aplica `allow_user_writes` en `_effective_tenant()` al arrancar cada worker (forzando solo lectura si el admin desactiva `MCP_REMOTE_ALLOW_USER_WRITES=false`, reflejándolo también en `/connect/status`).

**Tests añadidos:**
- `test_pinned_direct_route_in_login_customer_map_is_not_overridden_by_session_discovery`, `test_draining_worker_stays_counted_and_finishes_inflight_write_before_termination`, `test_disconnect_while_worker_draining_returns_202_and_defers_google_revocation` (distinción `HTTP 202 disconnecting` vs. `HTTP 200 disconnected` con revocación diferida en `on_drained` hasta concluir la última solicitud en vuelo) y verificación de rechazo cross-browser `oidc_browser_mismatch` + headers `no-store`/`no-referrer` en [`tests/test_remote_self_serve.py`](../tests/test_remote_self_serve.py) (**`565 passed`**).

### 2026-11-21 — Concurrencia de desconexión repetida, reconexión durante drenaje y ventana de revocación en Google

**Estado:** Completado y verificado (**`571 passed`**; 21 pruebas remotas en `test_remote_gateway.py` + `test_remote_self_serve.py`).

1. **Desconexión idempotente durante drenaje y revocación en vuelo (`agency/remote_store.py`, `agency/remote_onboarding.py`)**
   - **Qué se hizo:** `DynamicWorkerPool` diferencia ahora `_draining_workers` (solicitud en curso), `_finalizing_workers` (worker ya detenido con `on_drained`/revocación Google en curso) y `_pending_revocations` (revocación directa sin worker, registrada sincrónicamente antes del primer `await`). `is_disconnecting(subject)` permanece `True` hasta que terminan tanto el drenaje como la respuesta de `oauth2.googleapis.com/revoke`. Durante toda esa ventana, `/connect/status` mantiene `disconnecting: true` y `revocation_pending: true`, y cualquier reintento de `POST /connect/disconnect` sigue devolviendo `HTTP 202` (`status: "disconnecting"`) en lugar de un `200` prematuro.
   - **Por qué:** Cierra la carrera entre la terminación del proceso worker y la respuesta HTTP de revocación en Google.

2. **Bloqueo de reconexión y `disconnect_epoch` atómico en SQLite (`BEGIN IMMEDIATE`) (`agency/remote_store.py`, `agency/remote_onboarding.py`)**
   - **Qué se hizo:** `GET /connect/google/start`, `GET /connect/google/callback` y `POST /connect/configure` devuelven `HTTP 409 Conflict` (`error: "disconnect_in_progress"`, `Retry-After: 2`) mientras `pool.is_disconnecting(subject)` sea `True`. Además, `EncryptedProfileStore` mantiene `subject_disconnect_epochs` en modo WAL y ejecuta todas sus mutaciones (`save_oauth_pending`, `pop_oauth_pending`, `upsert_tenant`, `update_preferences`, `revoke_tenant`) dentro de transacciones explícitas `BEGIN IMMEDIATE` con `busy_timeout = 30000`, adquiriendo el lock de escritura de SQLite **antes** de leer `epoch` y escribir el grant o activar la conexión. Cada flujo `/connect/google/start` captura su `disconnect_epoch`, `google_callback` revalida `is_disconnecting` y `disconnect_epoch` tras `exchange_code`/`discover_accounts`, y tanto guardar el `grant_token` como activar el tenant en `upsert_tenant` validan atómicamente el epoch en SQLite.
   - **Por qué:** Garantiza atomicidad real incluso entre múltiples instancias o procesos que compartan `tenants.db` (evitando que una instancia lea `epoch=0`, otra desconecte subiendo a `epoch=1`, y la primera logre escribir un grant o activar un tenant obsoleto).

3. **Perfiles efímeros por instancia de worker (`agency/remote_store.py`, `google_ads_connect.py`)**
   - **Qué se hizo:** Cada worker lanzado por `DynamicWorkerPool` materializa su propio archivo cifrado `profile-<instance_id>.json` (almacenado en `_ManagedWorker.profile_path`) y al finalizar (`_finalize_worker`) elimina exclusivamente ese archivo (`profile_path.unlink(missing_ok=True)`), mientras `wipe_runtime_profile()` excluye siempre las rutas activas/en drenaje/finalizando. En `google_ads_connect.py:serve()`, `dir_prefix` normaliza `profile-*` a `profile` para que `profile-data`, `profile-config` y `profile-files` sigan compartiéndose entre reinicios del mismo usuario.
   - **Por qué:** Impide que el cierre de un worker antiguo borre el perfil cifrado de un worker nuevo en solapamientos de reconfiguración o pool.

**Tests añadidos:**
- `test_repeated_disconnect_while_draining_stays_202_and_preserves_profile_until_drained`, `test_reconnect_while_previous_worker_drains_preserves_new_worker_profile`, `test_disconnect_revocation_in_flight_keeps_disconnecting_status_and_blocks_reconnect`, `test_direct_disconnect_revocation_in_flight_without_worker_keeps_disconnecting_and_blocks_reconnect`, `test_inflight_google_oauth_callback_is_rejected_when_disconnect_starts_mid_exchange` y `test_cross_instance_sqlite_disconnect_epoch_is_atomic_for_grant_and_upsert` en [`tests/test_remote_self_serve.py`](../tests/test_remote_self_serve.py).
