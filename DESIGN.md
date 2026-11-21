# Diseño operativo (`google-ads-mcp-server`)

## Objetivo

`google-ads-mcp-server` permite que particulares, analistas, especialistas PPC y agencias consulten, auditen, diagnostiquen y operen cuentas de Google Ads (directas y multi-MCC) desde cualquier cliente MCP de escritorio sin saltarse controles humanos. El servidor conserva el entrypoint `google_ads_server.py` (con CLI unificado `connect`, `register`, `doctor`, `alerts`, `serve`, `--demo`, `--preset`, `--modules`) y distribuye las capacidades dentro del paquete `agency`.

## Arquitectura y componentes

- `google_ads_server.py`: entrypoint MCP (`stdio` y `streamable-http`), CLI unificado, modo `--demo` y 13 herramientas núcleo.
- `google_ads_connect.py`: wizard OAuth en navegador (`connect`), multi-cuenta/`--all-mcc-clients`, registro multi-cliente (`claude-desktop`, `claude-code`, `codex`, `antigravity`, `gemini-cli`, `generic`) y preflight `doctor`.
- `agency/runtime.py`: `RuntimeContext` (`ContextVar`), protocolo `StorageBackend` (`LocalFileStorageBackend` por defecto) y enrutamiento dinámico multi-MCC (`resolve_login_customer_id`).
- `agency/dates.py`: ventanas en zona horaria de cuenta (`ZoneInfo` + `tzdata`), días cerrados por defecto (`complete_days_only=True`) y metadatos de cobertura + *conversion lag*.
- `agency/api.py`: cliente REST con paginación acotada (`search_with_meta`, `max_rows` / `truncated`), retry, timeout, rate limiting y header `login-customer-id` dinámico por cuenta.
- `agency/modules.py`: catálogo modular (`core` ≤25 tools + módulos optativos `diagnostics`, `measurement`, `search`, `pmax`, `shopping`, `display`, `demand-gen`, `mcc`, `operations`) y presets (`core`, `leads`, `ecommerce`, `agency`, `full`), ocultando `operations` en `tools/list` mientras `READ_ONLY_MODE=true`.
- `agency/analysis_profiles.py`: perfiles de análisis personalizables (Markdown + TOML) con doble exposición (prompt MCP + tools `list_analysis_profiles` / `get_analysis_profile`).
- `agency/measurement_tools.py`: `audit_conversion_tracking` y `get_offline_conversion_diagnostics` (+ elegibilidad Data Manager API).
- `agency/diagnostics_tools.py`: `diagnose_delivery` (billing + políticas + estados), `get_budget_pacing`, landing pages, demografía y assets/calls.
- `agency/channels_tools.py`: `get_pmax_diagnostics`, `get_shopping_performance`, `get_placements_and_video_performance` y `analyze_auction_insights_csv`.
- `agency/operations_tools.py` + `agency/write_tools.py`: mutaciones guardarraíles por dominio.
- `agency/mutations.py` + `agency/governance.py` + `agency/locks.py`: contrato uniforme `validateOnly`, *optimistic locking* (`verify_not_stale`), locks de recurso, idempotencia, auditoría append-only con redacción de PII/secretos y reversión (`revert_operation`).
- `agency/reports.py` + `agency/cli_alerts.py`: reportes white-label tipados (`ReportBranding`), dashboard consolidado multi-moneda, `get_performance_trends`, waste y CLI ejecutable `alerts`.
- `agency/demo.py`: dataset sintético determinístico multi-moneda (`EUR` + `USD`) para exploración en <1 minuto sin credenciales.

## Invariantes

1. `READ_ONLY_MODE=true` por defecto; en modo solo lectura las tools de escritura (`operations`) **no se exponen** en `tools/list`.
2. Toda mutación inicia con `dry_run=true` (`validateOnly=true`) y emite un `approval_id` ligado a operador, cliente, TTL y hash del payload.
3. Antes de ejecutar `dry_run=false`, el *optimistic locking* (`state_verifier`) rechaza aprobaciones obsoletas si el estado en Google Ads cambió tras el preview.
4. `ALLOWED_CUSTOMER_IDS`, `BLOCKED_TOOLS`, caps económicos y aprobaciones se aplican exclusivamente en el servidor; ningún perfil de análisis puede relajarlos.
5. Los reportes nunca mezclan monedas distintas en agregados, usan días cerrados en la zona horaria de la cuenta y advierten sobre *conversion lag*.
6. Las lecturas protegen la ventana de contexto del LLM mediante `max_rows` + `truncated: true`, y todas las tools pasan el linter de JSON Schema multi-IA.
7. Toda tool queda auditada en el log append-only con redacción automática de secretos y PII.

## Estado de validación

La base cuenta con **531+ tests automatizados** en `tests/` (unitarios, regresión, higiene/privacidad, protocolo MCP sobre `stdio` y `streamable-http`, E2E `--demo` y linter multi-IA de JSON Schema) ejecutados en CI multiplataforma (`.github/workflows/ci.yml` en Ubuntu, macOS y Windows). Antes de publicar `v1.0.0`, completar los pilotos en cuentas reales descritos en [docs/release-checklist.md](docs/release-checklist.md).
