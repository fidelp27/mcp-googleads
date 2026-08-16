# Runbook de audiencias y assets

## Audiencias

- `get_audience_performance` aporta evidencia antes de cambiar targeting.
- `add_audience_to_ad_group` soporta USER_LIST y USER_INTEREST en TARGETING u OBSERVATION.
- `remove_audience_from_ad_group` elimina el criterion indicado.
- `update_bid_modifier` limita el rango a -90% / +900% y exige confirmación fuera de -50% / +100%.
- `create_customer_match_list` exige `consent_confirmed=true`; solo crea la lista vacía.

## Customer Match — gestión de miembros

Cuatro tools cierran el ciclo sobre `OfflineUserDataJobService`:

- `add_customer_match_members(customer_id, user_list_id, members, consent, dry_run)` — crea un job, agrega members en batches de 10k y lo corre. Devuelve `job_resource_name` para poder pollear estado.
- `remove_customer_match_members(customer_id, user_list_id, members, consent, dry_run)` — mismo pipeline con operaciones `remove`.
- `get_customer_match_list_stats(customer_id, user_list_id)` — tamaño, membership status y eligibility.
- `get_offline_user_data_job_status(customer_id, job_id)` — estado del job (`PENDING`/`RUNNING`/`SUCCESS`/`FAILED`) con `failure_reason` si aplica.

### Formato de `members`

Lista de dicts con **al menos uno** de:
- `email` — se normaliza (lowercase, sin puntos y `+` en Gmail) y se hashea SHA-256 antes de enviar.
- `phone` — se normaliza a E.164 y se hashea SHA-256.
- `hashed_email` / `hashed_phone` — ya hasheados, se pasan tal cual (validando forma hex64).
- `mobile_id` — GAID / IDFA, sin hashing.
- `third_party_user_id` — CRM ID, sin hashing.
- `address_info` — dict con `first_name`, `last_name`, `country_code`, `postal_code`. Nombre y apellido se hashean; código país en mayúsculas ISO alpha-2.

### Consent obligatorio

Cada call requiere `consent = {"ad_user_data": "GRANTED"|"DENIED", "ad_personalization": "GRANTED"|"DENIED"}`. `UNSPECIFIED` es rechazado — la agencia debe capturar el estado real antes de invocar la tool.

### Flujo de escritura

1. `add_customer_match_members(..., dry_run=true)` valida hashing + consent local; no toca la API. Devuelve `approval_id` y `expected_confirmation="ADD <N> MEMBERS"`.
2. Repetir con `dry_run=false`, `approval_id` y `confirmation_text="ADD <N> MEMBERS"`.
3. La ejecución crea el job, agrega ops en batches de 10k, lanza `run` y devuelve `job_resource_name`.
4. Poll con `get_offline_user_data_job_status(job_id)` hasta que pase de `RUNNING` a `SUCCESS` / `FAILED`.
5. `get_customer_match_list_stats(user_list_id)` para confirmar el tamaño resultante.

### Restricciones

- Máximo **100.000 members por call** (Google acepta más pero conviene batch en múltiples calls).
- `READ_ONLY_MODE=true` bloquea la ejecución real (dry-run sigue permitido).
- Datos PII redactados del audit log (los `members` nunca se persisten en `audit.jsonl`).

## Assets

- Crea sitelinks, callouts, snippets, llamadas e imágenes como recursos reutilizables.
- `create_image_asset` acepta URL o base64, nunca ambos, y limita descargas a 5 MB.
- `link_asset_to_campaign` y `unlink_asset_from_campaign` controlan asociaciones.
- `create_pmax_asset_group` crea el grupo, text assets y asociaciones multimedia en un batch con resource names temporales. Los assets de marca se vinculan a nivel campaña; usa `link_brand_assets=true` solo si la campaña todavía no los tiene.

Verifica tamaños, políticas editoriales y derechos de uso antes del dry-run. El hecho de que Google valide el payload no sustituye la revisión legal o de marca.
