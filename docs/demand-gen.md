# Runbook de Demand Gen

Google está discontinuando el `advertising_channel_type=DISPLAY` standalone
en favor de Demand Gen. Estas tools cubren el flujo mínimo para no depender
de la UI durante la migración: crear la campaña, crear el ad group con
channel controls, y leer performance.

## Tools

| Tool | Rol | Uso |
|---|---|---|
| `create_demand_gen_campaign(customer_id, name, budget_id, bidding_strategy_type, target_cpa_micros?, target_roas?, ...)` | Escritura | Crea campaña Demand Gen PAUSED. |
| `create_demand_gen_ad_group(customer_id, campaign_id, name, channel_strategy, channels?, cpc_bid_micros?, ...)` | Escritura | Crea ad group PAUSED con channel controls. |
| `get_demand_gen_performance(customer_id, campaign_id?, days=30)` | Lectura | Métricas por campaña + ad group + roll-up. |

## Bidding

`bidding_strategy_type` acepta:

- **`MAXIMIZE_CONVERSIONS`** — no requiere target adicional.
- **`TARGET_CPA`** — requiere `target_cpa_micros`. El cap `MAX_BID_MICROS` se aplica al target.
- **`TARGET_ROAS`** — requiere `target_roas` (float; `3.5` = 350% ROAS).

## Channel controls

`channel_strategy` acepta:

- **`ALL_CHANNELS`** (default) — Demand Gen decide qué canales usar.
- **`SELECTED_CHANNELS`** — requiere lista `channels` con al menos un valor de:
  - `youtube_shorts`, `youtube_in_feed`, `youtube_in_stream`
  - `discover`, `gmail`, `display`, `maps`

Los nombres se normalizan a lowerCamelCase para el payload de la API.

## Flujo típico

1. `create_campaign_budget(name, daily_amount_micros, ..., dry_run=true)` → aprobar y crear.
2. `create_demand_gen_campaign(name, budget_id, bidding_strategy_type=..., ..., dry_run=true)` → aprobar con `confirmation_text=<name>`, crear (queda PAUSED).
3. `create_demand_gen_ad_group(campaign_id, name, channel_strategy=..., ..., dry_run=true)` → aprobar con `confirmation_text=<name>`, crear (queda PAUSED).
4. Cargar creativos desde la UI (Demand Gen tiene 4 subtypes de ads con requerimientos multi-asset; la creación vía API queda para follow-up).
5. `enable_campaign` (tool existente) para activar cuando esté todo listo.
6. Después de que la campaña sirva unos días: `get_demand_gen_performance(days=7)` para primer read.

## Restricciones de v1

- No incluye creación de ads (Demand Gen tiene 4 subtypes con validación compleja de assets 1:1 / 4:5 / 9:16 / 16:9; queda para follow-up).
- No incluye audience targeting (los signals de audience son parámetro del ad group; se puede añadir en v1.1).
- Las campañas se crean siempre PAUSED; usar `enable_campaign` para activarlas.

## Errores frecuentes

- `Unrecognized field 'metrics.video_views'` en la query base fue arreglado en v1.
- `Unknown enum value DEMAND_GEN`: usar minúsculas o mayúsculas — la enum acepta `'DEMAND_GEN'` (con guión bajo).
- Bidding strategy incompatible: Demand Gen no soporta `MANUAL_CPC`; usar sólo `MAXIMIZE_CONVERSIONS`, `TARGET_CPA` o `TARGET_ROAS`.
