# Ejemplos GAQL compatibles con Google Ads API v25

Estos ejemplos usan campos conservadores y recursos vigentes. Antes de ampliar una consulta, utiliza `list_resources` o GoogleAdsFieldService para comprobar compatibilidad entre campos.

## Rendimiento de campañas

```sql
SELECT
  campaign.id,
  campaign.name,
  campaign.status,
  metrics.impressions,
  metrics.clicks,
  metrics.ctr,
  metrics.average_cpc,
  metrics.cost_micros,
  metrics.conversions,
  metrics.conversions_value,
  metrics.conversions_value_per_cost
FROM campaign
WHERE segments.date DURING LAST_30_DAYS
ORDER BY metrics.cost_micros DESC
LIMIT 100
```

## Rendimiento de palabras clave

```sql
SELECT
  campaign.name,
  ad_group.name,
  ad_group_criterion.keyword.text,
  ad_group_criterion.keyword.match_type,
  ad_group_criterion.status,
  metrics.impressions,
  metrics.clicks,
  metrics.ctr,
  metrics.average_cpc,
  metrics.cost_micros,
  metrics.conversions
FROM keyword_view
WHERE
  segments.date DURING LAST_30_DAYS
  AND ad_group_criterion.status != 'REMOVED'
ORDER BY metrics.clicks DESC
LIMIT 200
```

## Rendimiento de anuncios

```sql
SELECT
  campaign.name,
  ad_group.name,
  ad_group_ad.ad.id,
  ad_group_ad.ad.type,
  ad_group_ad.status,
  metrics.impressions,
  metrics.clicks,
  metrics.ctr,
  metrics.cost_micros,
  metrics.conversions
FROM ad_group_ad
WHERE
  segments.date DURING LAST_30_DAYS
  AND ad_group_ad.status != 'REMOVED'
ORDER BY metrics.impressions DESC
LIMIT 200
```

## Creatividades de búsqueda responsiva

```sql
SELECT
  campaign.name,
  ad_group.name,
  ad_group_ad.ad.id,
  ad_group_ad.ad.final_urls,
  ad_group_ad.ad.responsive_search_ad.headlines,
  ad_group_ad.ad.responsive_search_ad.descriptions
FROM ad_group_ad
WHERE
  ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
  AND ad_group_ad.status != 'REMOVED'
LIMIT 100
```

## Assets de imagen

```sql
SELECT
  asset.id,
  asset.name,
  asset.type,
  asset.image_asset.mime_type,
  asset.image_asset.full_size.url,
  asset.image_asset.full_size.width_pixels,
  asset.image_asset.full_size.height_pixels
FROM asset
WHERE asset.type = 'IMAGE'
ORDER BY asset.id
LIMIT 200
```

## Cuenta y moneda

```sql
SELECT
  customer.id,
  customer.descriptive_name,
  customer.currency_code,
  customer.time_zone
FROM customer
LIMIT 1
```

## Fechas personalizadas

Para períodos no incluidos en los rangos predefinidos:

```sql
SELECT
  campaign.name,
  metrics.clicks,
  metrics.cost_micros
FROM campaign
WHERE segments.date BETWEEN '2026-06-01' AND '2026-06-30'
ORDER BY metrics.cost_micros DESC
```

Los costos están expresados en micros: `1_000_000` equivale a una unidad de la moneda de la cuenta.
