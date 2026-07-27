# Runbook de audiencias y assets

## Audiencias

- `get_audience_performance` aporta evidencia antes de cambiar targeting.
- `add_audience_to_ad_group` soporta USER_LIST y USER_INTEREST en TARGETING u OBSERVATION.
- `remove_audience_from_ad_group` elimina el criterion indicado.
- `update_bid_modifier` limita el rango a -90% / +900% y exige confirmación fuera de -50% / +100%.
- `create_customer_match_list` exige `consent_confirmed=true`; solo crea la lista vacía.

## Assets

- Crea sitelinks, callouts, snippets, llamadas e imágenes como recursos reutilizables.
- `create_image_asset` acepta URL o base64, nunca ambos, y limita descargas a 5 MB.
- `link_asset_to_campaign` y `unlink_asset_from_campaign` controlan asociaciones.
- `create_pmax_asset_group` crea el grupo, text assets y asociaciones multimedia en un batch con resource names temporales. Los assets de marca se vinculan a nivel campaña; usa `link_brand_assets=true` solo si la campaña todavía no los tiene.

Verifica tamaños, políticas editoriales y derechos de uso antes del dry-run. El hecho de que Google valide el payload no sustituye la revisión legal o de marca.
