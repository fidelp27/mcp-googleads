# Runbook de campañas

## Lectura antes de actuar

Ejecuta `get_client_context`, `get_account_currency`, `get_campaign_performance`, `get_budget_utilization` y, si existe un incidente, `get_change_history`.

## Crear estructura

1. `create_campaign_budget` con `dry_run=true`.
2. Confirma el nombre exacto y ejecuta con el `approval_id`.
3. Usa `create_search_campaign`, `create_pmax_campaign` o `create_display_campaign`.
4. La campaña queda `PAUSED` salvo que se solicite y confirme explícitamente `force_enable`.
5. Crea ad groups, ads, keywords y assets.
6. Verifica todo antes de `enable_campaign`.

Los presets disponibles se configuran en `config/presets.json`. Los templates viven en `config/templates.json` y se aplican con `create_campaign_from_template`.

Google Ads v25 habilita brand guidelines en campañas Performance Max nuevas. `create_pmax_campaign` exige assets existentes para `BUSINESS_NAME`, `LOGO` y `LANDSCAPE_LOGO` y los vincula en el mismo batch. La expansión de URL se configura mediante `assetAutomationSettings`, no con el campo retirado `urlExpansionOptOut`.

## Cambios de estado

- `pause_campaign`: reversible; conserva preview y auditoría.
- `enable_campaign`: exige escribir `nombre | presupuesto_diario_micros`.
- `remove_campaign`: destructiva; exige el nombre exacto.
- `update_campaign`: un cambio de bidding exige el nombre exacto.
- `copy_campaign`: clona en estado pausado y utiliza IDs temporales para evitar duplicados parciales.

## Errores comunes

- `READ_ONLY_MODE=true`: las mutations están deshabilitadas.
- Cuenta no permitida: agrega MCC y cliente a `ALLOWED_CUSTOMER_IDS`.
- Payload distinto: genera un nuevo dry-run.
- Approval expirado: repite la validación.
- Budget o bid excedido: corrige el valor o cambia el cap con autorización del dueño.
