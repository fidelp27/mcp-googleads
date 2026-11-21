# Roadmap v3 de Google Ads MCP

**21 de noviembre de 2026 · Proyecto open source de escritorio**

La primera beta debe permitir que una persona instale el MCP fuera del repositorio, inicie una demo sin credenciales, conecte su propia cuenta, seleccione los módulos y ejecute un análisis con un perfil que pueda editar. La beta se distribuye en modo de solo lectura y usa `stdio` como transporte principal. El catálogo seguirá creciendo por canal y las escrituras se habilitarán por familia conforme se verifiquen.

## Estado de las seis fases

| Fase | Estado tras la revisión | Qué falta para cerrarla |
| --- | --- | --- |
| **0 · Instalación y privacidad** | Wheel construido fuera del checkout con los tres perfiles y `google_ads_cli.py`. Arranque directo `google-ads-mcp-server [--profile ...] serve` resuelto y validado en E2E `stdio`. Launchers, contexto Docker y bind HTTP corregidos. | Completar CI en Windows, macOS y Linux en GitHub Actions, ejecutar build Docker real y verificar/limpiar el historial Git de la distribución pública. |
| **1 · Módulos, perfiles y datos** | Catálogo modular, presets, configuración Markdown/TOML y señalización de fechas cerradas, zona horaria, `truncated` y `totals_are_partial` en reportes, tendencias, waste, pacing, entrega, canales y auditoría de conversiones (incluyendo consultas de configuración y métricas). | Completar el recorrido de aceptación desde una instalación limpia y validar los informes contra cuentas reales. |
| **2 · Medición** | Google Ads v25 aceptó `conversion_goal_campaign_config`; `audit_conversion_tracking` y `get_offline_conversion_diagnostics` propagan `truncated`, `truncated_queries`, `totals_are_partial` y `coverage_complete` tanto en consultas de configuración (`conversion_action`, objetivos) como de métricas. | Contrastar el informe completo en casos reales de herencia de cuenta, objetivo personalizado, acción secundaria incluida en ese objetivo y seguimiento centralizado mediante MCC. Probar también el diagnóstico offline cuando haya datos. |
| **3 · Diagnóstico y canales** | Cobertura implementada para entrega, pacing, Search, PMax, Shopping, placements, vídeo y alertas. Google Ads v25 aceptó `asset_group_asset.primary_status`. `get_pmax_diagnostics` ya propaga truncamiento API paginado de todas sus subconsultas (`asset_group`, `asset_group_asset`, `asset_group_signal`, `asset_group_listing_group_filter`, insights y placements) y distingue `complete`, `empty`, `sampled` y `partial_error`. | Validar insights de términos de búsqueda con datos reales de PMax y contrastar cobertura por canal en pilotos reales. |
| **4 · Escrituras** | Protección frente al estado obsoleto (`state_verifier`) en operaciones core y de conversión, reintento parcial MCC, reversión ampliada y matriz explícita de garantías por familia publicada en `docs/compatibility-matrix.md`. | Validar reintentos, conflictos, verificación posterior y reversión en cuentas de prueba reales. Esta fase puede avanzar después de la beta de lectura. |
| **5 · Compatibilidad y publicación** | Documentación EN/ES, checklist, matriz de garantías por familia y configuración para varios clientes. La matriz marca `.mcpb` como planificado. | Ejecutar las pruebas en aplicaciones reales, completar los tres pilotos técnicos y preparar los artefactos de publicación. |

## Evidencia de la revisión

| Comprobación | Resultado |
| --- | --- |
| Regresiones dirigidas | **14 aprobadas y 3 excluidas**, incluidas las **3 pruebas de higiene**. |
| Suite completa tras fixes v3 | **`550 passed in 34.21s`** localmente en Windows (Python 3.12.5), incluyendo `tests/test_audit_fixes.py` (19 pruebas: wheel aislado con `google_ads_cli.py`, E2E `stdio` de `google_ads_cli.py` y `google_ads_server.py` con `--profile ... serve`, PMax `empty`/`sampled`/`partial_error`, truncamiento paginado en PMax/conversiones/entrega, degradación a `insufficient_data` de alertas basadas en ausencia ante truncamiento, y `state_verifier`). Sin ninguna referencia a endpoints de Microsoft o telemetría en el repositorio. |
| Instalación aislada | El wheel incluyó seis archivos Markdown/TOML. Se pudieron cargar `leads-audit`, `ecommerce-audit` y `agency-weekly` fuera del checkout. |
| Lecturas reales v25 | La API aceptó las consultas de objetivos por campaña y assets actualizadas. La prueba no encontró campañas PMax activas en las cuentas disponibles para comprobar los insights con datos reales. |
| Documentación | La sección 11 del v2 y la matriz contienen los cambios de Gemini. El Word local tiene el anexo posterior a la auditoría. La copia de ChatGPT consultada todavía mostraba el documento original de cinco páginas. |

## P0 de la beta de escritorio

| Prioridad | Pendiente | Criterio de cierre |
| --- | --- | --- |
| **P0 · Resuelto en código** | **Arranque con perfil gestionado.** Se introdujo el dispatcher liviano `google_ads_cli.py` como entrypoint de `google-ads-mcp-server` / `google-ads-mcp` (antes de importar `google_ads_server` o `agency`) más `apply_managed_profile()` en `google_ads_server.py`. Verificado en E2E `stdio` (`tests/test_audit_fixes.py`) para `google_ads_cli.py` y `google_ads_server.py`, con env de señuelo y `READ_ONLY_MODE=true`. | Cerrado en código (`546 passed`). Queda únicamente repetir el smoke test manual tras instalar desde wheel/PyPI en una cuenta viva. |
| **P0 · Resuelto en código (pendiente piloto PMax real)** | **Cobertura de Performance Max.** `get_pmax_diagnostics` propaga ahora truncamiento API paginado de todas sus subconsultas (`asset_group`, `asset_group_asset`, `asset_group_signal`, `asset_group_listing_group_filter`, `campaign_search_term_insight` y `performance_max_placement_view`) y expone `coverage_status`, `truncated`, `truncated_queries`, `search_term_insights_coverage`, `placements_coverage` y `sampling_warnings`, marcando `coverage_complete: false` ante cualquier muestreo, truncamiento API o error. | Cerrado en código con tests para los 5 escenarios (`empty`, 12 campañas, 25/50 filas, `partial_error` y respuesta paginada truncada en `asset_group`/`asset_group_asset`). Sigue abierta la validación con cuenta real que tenga PMax activo. |
| **P0 · Pendiente CI** | **Instalación y CI reproducibles.** Verificar los cambios de Windows y empaquetado en entornos limpios. | Guardar resultados de CI para Windows, macOS y Linux en GitHub Actions. Probar wheel instalado fuera del repositorio, demo sin credenciales y flujo de perfil gestionado. |
| **P0 · Pendiente manual** | **Privacidad de la distribución.** El historial conserva archivos internos y existe un informe local de cliente sin rastrear. | Revisar el historial que vaya a publicarse, el árbol final y los artefactos para excluir datos de clientes y credenciales. Usar un historial nuevo y limpio o acordar explícitamente una reescritura del historial existente. |
| **P0 · Pendiente externo** | **Compatibilidad declarada y publicación.** La configuración de un cliente y la prueba del proceso MCP son verificaciones distintas de su uso desde la aplicación. | Registrar cliente, versión, sistema operativo, transporte y evidencia del recorrido completo. Publicar la beta de lectura cuando estén cerrados los P0 externos y se hayan verificado los pilotos. |

## Recorrido de aceptación

1. Instalar el wheel desde un directorio ajeno al repositorio y arrancar la demo sin credenciales.
2. Conectar una cuenta autorizada mediante `connect` y comprobar el resultado de `doctor`. Repetir con una cuenta directa y una administrada por MCC.
3. Iniciar la sesión mediante el comando registrado y mediante `serve`. Comprobar que el preset y el alcance de la cuenta coinciden en ambas vías.
4. Editar una plantilla Markdown y parámetros TOML de usuario. Recuperar el perfil por prompt MCP cuando el cliente lo admita y por `list_analysis_profiles` / `get_analysis_profile` cuando use herramientas. El informe identifica el perfil y los parámetros efectivos.
5. Ejecutar un informe y contrastar con la interfaz de Google Ads usando las mismas fechas cerradas, zona horaria, moneda, columnas y filtros. Verificar que los límites de filas se informen y que no se mezclen monedas en un total.

| Cliente | Evidencia requerida para anunciar soporte |
| --- | --- |
| Claude Desktop y Claude Code | Probar las dos vías por separado, con registro, inicio, descubrimiento de herramientas y ejecución del informe. Probar el bundle `.mcpb` si se ofrece. |
| Codex | Registrar por separado las superficies que se anuncien como compatibles. Verificar una llamada de herramienta y el perfil aplicado. |
| Antigravity y Gemini CLI | Probar alta y ejecución en cada aplicación y registrar la versión utilizada. |
| DeepSeek y Kimi | Identificar el host de escritorio, su versión y el modelo usado para ejecutar el mismo informe por MCP. |
| Otros clientes | Documentar el bloque `--client generic` y marcar como verificadas las combinaciones efectivamente probadas. |

## Pilotos técnicos

| Piloto | Verificación |
| --- | --- |
| **Search y leads** | Auditar objetivos de conversión, entrega y gasto con `leads-audit`. Confirmar que el informe distinga hechos, hipótesis y datos faltantes. |
| **Ecommerce** | Usar `ecommerce-audit` con PMax y Shopping. Contrastar assets, insights de términos de búsqueda, cobertura y pacing con datos reales. |
| **MCC y varias monedas** | Usar `agency-weekly`, consultar cuentas directas y administradas, separar los totales por moneda y ejecutar las alertas por CLI. |

## P1 de exactitud y escrituras

- **Resuelto en código (propagación de truncamiento y degradación de alertas basadas en ausencia):** las subconsultas de configuración y métricas en `get_pmax_diagnostics`, `audit_conversion_tracking`, `get_offline_conversion_diagnostics` y `diagnose_delivery` —junto con `get_budget_pacing`, `get_assets_and_calls_performance`, `_report_data`, `generate_agency_dashboard`, `compare_periods`, `get_performance_trends`, `detect_waste`, `get_shopping_performance` y `get_placements_and_video_performance`— propagan `truncated`, `totals_are_partial` / `truncated_queries`, `truncation_warning` y `coverage_complete: false`. Además, las conclusiones basadas en ausencia (`SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS`, `PRIMARY_ACTION_NO_VOLUME`, `NO_RECENT_OFFLINE_UPLOADS` y `NO_APPROVED_BILLING_SETUP`) se suprimen y degradan automáticamente a `insufficient_data` cuando la consulta que las sustenta se trunca o falla una subconsulta opcional (un error API en la consulta principal `conversion_action` sigue propagando `GoogleAdsError` intencionalmente en lugar de emitir un informe vacío). En `get_pmax_diagnostics`, `truncated_queries` cubre las 6 subconsultas, `search_term_insights_coverage.capped_by_api_row_limit` refleja insights/`asset_group` y `placements_coverage.capped_by_api_row_limit` refleja placements. Otros consumidores secundarios de `search()` fuera de estas herramientas de informe/auditoría se revisarán si se amplían a agregaciones.
- Mantener los días cerrados y la zona horaria de la cuenta como referencia para los informes. Verificar conversión por fecha del evento y por fecha de la interacción en los informes que exponen ambas series en pilotos reales.
- **Documentado por familia:** la sección 5 de [compatibility-matrix.md](compatibility-matrix.md) ahora detalla exactamente qué garantías aplica cada familia de escritura (`write_tools`, `operations_tools`, `experiment_tools`, `demand_gen_tools`, `customer_match_tools`, `offline_conversions_tools` y `mcc_tools`). Queda pendiente probar en cuenta de prueba real conflictos de concurrencia, reintentos repetidos de `mcc_bulk_apply` y reversión de punta a punta.

## Lanzamiento y trabajo posterior

- **Beta open source.** Preparar versión y changelog, revisar el contenido del wheel y comprobar una instalación limpia con `uvx` una vez publicado el paquete. Actualizar la matriz con evidencia de los clientes y canales ofrecidos. Mantener permisos de cuenta, límites de gasto y aprobaciones fuera del alcance de los perfiles editables.
- **HTTP y contenedor.** El código usa loopback por defecto y rechaza un bind no local salvo activación explícita. La bandera de activación permite el bind. La autenticación corresponde al proxy o a la capa de acceso configurada. Antes de distribuir este modo, verificar el build real de Docker, la conectividad prevista y el rechazo de solicitudes sin autorización detrás de HTTPS.
- **Cobertura posterior.** Mantener las extensiones de escritorio y los clientes adicionales en la matriz. Las integraciones Merchant API, Data Manager API, GA4, GTM y CRM, los flujos especializados y el acceso remoto con OAuth siguen como posibles contribuciones al proyecto open source según las necesidades y pruebas disponibles.

## Referencias del repositorio

- [Roadmap v2 y bitácora de implementación](roadmap-marketing-v2.md)
- [Matriz de compatibilidad](compatibility-matrix.md)
- [Guía de conexión](connect.md)
- [Checklist de lanzamiento](release-checklist.md)
- [Regresiones posteriores a la auditoría](../tests/test_audit_fixes.py)
