# Keyword Planner

Cuatro tools sobre `KeywordPlanIdeaService` para investigar keywords sin gastar presupuesto. Ninguna toca la cuenta; sólo consultan.

| Tool | Endpoint | Uso |
|---|---|---|
| `generate_keyword_ideas` | `keywordPlanIdeas:generateKeywordIdeas` | Sugerencias a partir de semillas (keywords, URL o ambas). |
| `generate_keyword_historical_metrics` | `keywordPlanIdeas:generateKeywordHistoricalMetrics` | Búsquedas mensuales y competencia para una lista exacta. |
| `generate_keyword_forecast` | `keywordPlanIdeas:generateKeywordForecastMetrics` | Forecast de clics, impresiones, costo y conversiones al bid + budget indicados. |
| `find_geo_target_constants` | `geo_target_constant` (GAQL) | Traduce nombres de país/región al ID que exigen las otras tres. |

## Parámetros comunes

- `geo_target_ids`: uno o más criterion IDs, separados por coma o lista. Ejemplos: `2032` Argentina, `2484` México, `2724` España, `2840` EE.UU. Buscá con `find_geo_target_constants`.
- `language_id`: constant ID del idioma. Por defecto `1003` (español). Inglés: `1000`. Portugués: `1014`.
- `network`: `GOOGLE_SEARCH` (por defecto) o `GOOGLE_SEARCH_AND_PARTNERS`.
- Todos los importes vuelven en **micros** (1 unidad = 1.000.000 micros).

## Secuencia recomendada

1. `find_geo_target_constants` con `country_code="AR"` para confirmar el geo.
2. `generate_keyword_ideas` con 3–10 semillas y `keyword_annotations="KEYWORD_CONCEPT"` para agrupar por tema.
3. Seleccioná manualmente los términos con intención comercial y volumen razonable.
4. `generate_keyword_historical_metrics` con esa lista corta para ver estacionalidad y competencia mes a mes.
5. `generate_keyword_forecast` con el `cpc_bid_micros` y `daily_budget_micros` que estás evaluando para tener una proyección de clics/costo/conv antes de armar la campaña.

## Ejemplo · Cuenta demo (customer 1234567890, español, LatAm)

```text
generate_keyword_ideas(
  customer_id="1234567890",
  keywords="auditoría seo, auditoria seo, seo audit",
  geo_target_ids="2032,2484,2724",
  language_id="1003",
  keyword_annotations="KEYWORD_CONCEPT",
  page_size=100,
)
```

Devuelve por cada idea: `text`, `keywordIdeaMetrics.avgMonthlySearches`, `competition` (LOW/MEDIUM/HIGH), `competitionIndex` (0–100), `lowTopOfPageBidMicros`, `highTopOfPageBidMicros` y, si se pidió `KEYWORD_CONCEPT`, `keywordAnnotations.concepts`.

## Limitaciones

- `generate_keyword_ideas` acepta hasta **20 semillas**. Con más, dividí en varias llamadas.
- `generate_keyword_historical_metrics` acepta hasta **10.000 keywords** por request.
- El forecast asume una sola bidding strategy (`manual_cpc`) — para portafolios distintos, hay que armar la request a mano.
- Ninguna tool escribe: Keyword Planner no consume budget ni requiere aprobación.

## Errores frecuentes

- `INVALID_ARGUMENT: no valid seed`: pasar `keywords` **o** `page_url`.
- `KEYWORD_PLAN_ERROR: KEYWORD_PLAN_CAMPAIGN_HAS_NO_ADS`: el forecast requiere al menos una keyword.
- `INVALID_COUNTRY_CODE`: `find_geo_target_constants` sólo acepta ISO-3166 alpha-2 en mayúsculas (`AR`, no `ARG`).
