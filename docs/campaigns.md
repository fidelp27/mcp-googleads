# Runbook de campañas

## Lectura antes de actuar

Ejecuta `get_client_context`, `get_account_currency`, `get_campaign_performance`, `get_budget_utilization` y, si existe un incidente, `get_change_history`.

## Diagnóstico creativo — Ad Strength

`get_ad_strength_diagnostics(customer_id, campaign_id?, days=30, min_impressions=0)` audita cada RSA y su calidad de assets.

Ejecutar antes de tocar creatividades, antes de escalar presupuesto y como paso 1 de cualquier review trimestral.

### Qué devuelve

- **summary:** `total_ads`, `by_strength` (POOR/AVERAGE/GOOD/EXCELLENT/UNKNOWN), `assets_by_label` (PENDING/LEARNING/LOW/GOOD/BEST), `ads_with_pinned_assets`.
- **ads:** por RSA — campaign, ad_group, ad_id, status, `ad_strength`, `path_display`, métricas del período (impressions/clicks/conversions/CTR), detalle de headlines y descriptions con contador por label + items individuales (text, label, pinned_to, approved), y `suggestions` accionables.
- **recommendations:** conclusiones a nivel cuenta.

### Códigos de sugerencia (por ad)

| Código | Cuándo se dispara |
|---|---|
| `FEW_HEADLINES` | Menos de 8 headlines (high si <5). |
| `FEW_DESCRIPTIONS` | Menos de 3 descriptions (high si <2). |
| `AD_STRENGTH_POOR` | Strength POOR reduce elegibilidad en subasta. |
| `AD_STRENGTH_AVERAGE` | Strength AVERAGE, dejar impresiones sobre la mesa. |
| `LOW_PERFORMING_ASSETS` | Uno o más headlines/descriptions LOW. |
| `NO_IMPRESSIONS_IN_WINDOW` | Sin serving; labels quedan PENDING. |
| `INSUFFICIENT_SERVING_HISTORY` | <200 impressions con assets PENDING. |
| `OVER_PINNED_HEADLINES` | Todos los headlines pinneados; Google no puede rotar. |
| `BEST_HEADLINES_WITHOUT_PINS` | Hay BEST sin pinnear (candidato a posición 1). |

### Códigos a nivel cuenta

| Código | Cuándo |
|---|---|
| `POOR_STRENGTH_SHARE` | Al menos 1 RSA en POOR. |
| `MAJORITY_AVERAGE` | ≥50% en AVERAGE. |
| `LOW_ASSETS_ACROSS_ACCOUNT` | Assets LOW acumulados en múltiples ads. |
| `ALL_PENDING` | Toda la cuenta sin serving history todavía. |
| `NO_ADS_MATCHED_FILTER` | El filtro no devolvió RSAs. |

### Ejemplo

```text
get_ad_strength_diagnostics(customer_id="1234567890", days=30)
```

Devuelve 7 RSAs (1 AVERAGE, 6 sin scorear), 92 assets PENDING, recomendación `ALL_PENDING`.

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
