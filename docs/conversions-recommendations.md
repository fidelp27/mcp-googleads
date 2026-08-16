# Runbook de conversiones y recomendaciones

## Conversiones

Antes de optimizar bids, ejecuta `get_conversion_actions` y `get_conversion_performance`. Una cuenta con tracking incompleto no debe migrarse a estrategias automáticas por conversiones.

- `create_conversion_action`: crea una conversión web.
- `update_conversion_action`: modifica campos explícitamente permitidos.
- `pause_conversion_action`: la oculta para evitar que siga utilizándose operativamente.

Los cambios de conversiones requieren dry-run, confirmación y verificación posterior.

## Enhanced Conversions — auditoría

`get_enhanced_conversions_status(customer_id, days=30)` audita el setup completo de Enhanced Conversions y emite alertas priorizadas antes de que el problema afecte a Smart Bidding.

Ejecutar como primer paso de cualquier review, y siempre antes de aumentar presupuesto.

### Qué devuelve

- **tracking:** `conversion_tracking_id`, `status`, `google_ads_conversion_customer`, `accepted_customer_data_terms`, `enhanced_conversions_for_leads_enabled`.
- **conversion_actions:** por cada acción no `REMOVED` — id, name, status, type, category, origin, `primary_for_goal`, `included_in_conversions_metric`, `attribution_model`, `data_driven_model_status`, ventanas de lookback y métricas (`conversions`, `all_conversions`, `conversions_value`) del período.
- **alerts:** lista priorizada (critical / high / warning / info) con `code` y `message`.
- **summary:** contadores rápidos para dashboards.

### Códigos de alerta

| Código | Severidad | Cuándo se dispara |
|---|---|---|
| `CUSTOMER_DATA_TERMS_NOT_ACCEPTED` | critical / warning | Términos no aceptados. Critical si además existen acciones LEAD. |
| `EC4L_DISABLED_WITH_LEAD_ACTIONS` | high | Hay acciones LEAD pero `enhanced_conversions_for_leads_enabled` es false. |
| `NO_PRIMARY_FOR_GOAL` | high | Ninguna acción ENABLED marcada como `primary_for_goal`. Smart Bidding no puede optimizar. |
| `NO_CONVERSIONS_IN_WINDOW` | info | Cero conversiones en la ventana con acciones ENABLED. Validar tag firing. |
| `DDA_NOT_AVAILABLE` | warning | Acción primary con `data_driven_model_status` distinto de `AVAILABLE`. |
| `PRIMARY_NOT_COUNTED` | warning | Acción primary con `included_in_conversions_metric=false`. Inconsistente. |

### Ejemplo (Clariola)

```text
get_enhanced_conversions_status(customer_id="5461642152", days=30)
```

Devuelve 3 conversion actions, 2 alerts (`CUSTOMER_DATA_TERMS_NOT_ACCEPTED` + `NO_CONVERSIONS_IN_WINDOW`) y summary con `customer_data_terms_accepted=false`.

## Consent signal coverage — proxy vía attribution rate

`check_consent_signal_coverage(customer_id, days=30)` es el proxy honesto para detectar degradación de Consent Mode / EC. La Google Ads API **no expone per-evento** `ad_user_data` ni `ad_personalization` — esos parámetros viven en el `gtag('consent', ...)` client-side. Lo que **sí** entrega la API es el ratio `conversions / all_conversions` por campaña, comparable período vs período previo. Cuando la ratio cae abruptamente, casi siempre es tag o consent roto.

### Qué devuelve

- **tracking:** `accepted_customer_data_terms`, `enhanced_conversions_for_leads_enabled`, `conversion_tracking_status`.
- **campaigns:** por cada campaña con actividad — attributed / total conversions, attribution rate y delta en puntos porcentuales vs el período anterior de igual longitud.
- **summary:** `account_attribution_rate`, totales, N campañas evaluadas.
- **alerts:** priorizadas.
- **notes:** disclaimer explícito sobre la limitación de la API.

### Alertas

| Código | Severidad | Cuándo |
|---|---|---|
| `ZERO_ATTRIBUTED_WITH_TOTAL` | critical | Total > 0 pero attributed = 0. gclid o Consent Mode roto seguro. |
| `LOW_ATTRIBUTION_RATE` | high | Rate < 30% con ≥5 conversiones totales. |
| `ATTRIBUTION_DROP` | high | Caída ≥20pp vs período anterior. |
| `CUSTOMER_DATA_TERMS_NOT_ACCEPTED` | warning | Términos no aceptados a nivel cuenta. |
| `UNEXPECTED_TRACKING_STATUS` | info | `conversion_tracking_status` fuera del conjunto conocido. |
| `NO_DATA_TO_ASSESS` | info | Cero conversiones en ambos períodos. |

### Cuándo ejecutar

- Después de subir cambios en el tag / GTM.
- Antes de aumentar presupuesto significativo.
- Weekly en accounts productivas — es la señal más temprana de degradación de Smart Bidding.

## Recomendaciones

1. `list_recommendations`.
2. `get_recommendation_detail`.
3. Evalúa impacto, supuestos y objetivos del cliente.
4. `apply_recommendation` con preview y confirmación exacta, o `dismiss_recommendation`.

Las recomendaciones de Google no son órdenes. Presupuesto, bidding y expansión de targeting pueden tener impacto financiero y deben pasar los mismos guardarraíles que un cambio manual.
