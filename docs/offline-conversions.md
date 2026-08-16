# Runbook de Offline Conversions

Tres tools sobre `ConversionUploadService` y
`ConversionAdjustmentUploadService`:

- `upload_click_conversions(customer_id, conversions[], ...)` — sube conversiones que se conectan al clic vía `gclid`/`gbraid`/`wbraid` o (para Enhanced Conversions for Leads) por `user_identifiers` hasheados.
- `upload_call_conversions(customer_id, conversions[], ...)` — sube conversiones de llamada matcheando por `caller_id` (E.164) + `call_start_date_time`.
- `upload_conversion_adjustments(customer_id, adjustments[], ...)` — sube retracciones, restatements (cambios de valor) y enhancements (upgrade con datos de usuario).

## Consent obligatorio

Cada click/call conversion requiere `consent = {ad_user_data, ad_personalization}` con `GRANTED` o `DENIED`. `UNSPECIFIED` es rechazado — la agencia debe capturar el estado real antes de invocar. Los adjustments no llevan consent (por diseño de la API), pero los `ENHANCEMENT` sí hashean identifiers client-side.

## PII y hashing

Los `user_identifiers` (email, phone, address_info) se hashean SHA-256 client-side reusando el pipeline de `customer_match_tools`. Los `caller_id` en call conversions NO se hashean (la API los espera en claro E.164).

Los payloads con `conversions` / `adjustments` **no se persisten en el audit log** — sólo el `record_count` queda en `audit.jsonl`.

## Guardarraíles

- Máximo **2.000 records por call**.
- `READ_ONLY_MODE=true` bloquea la ejecución (dry-run sigue permitido).
- `partial_failure=true` por default → la API devuelve errores por-fila en `partialFailureError` en vez de tumbar todo el batch.
- `validate_only=true` corre la validación de Google sin persistir nada — recomendado antes del primer upload productivo.

## Formato

### Click conversion

```json
{
  "conversion_action_id": "7693341777",
  "conversion_date_time": "2026-08-16 12:00:00-03:00",
  "conversion_value": 99.99,
  "currency_code": "USD",
  "gclid": "Cj0KCQjw...",
  "order_id": "ORDER-42",
  "custom_variables": [
    {"conversion_custom_variable_id": "9", "value": "premium"}
  ],
  "consent": {"ad_user_data": "GRANTED", "ad_personalization": "GRANTED"}
}
```

Alternativa (Enhanced Conversions for Leads sin gclid):

```json
{
  "conversion_action_id": "7693341777",
  "conversion_date_time": "2026-08-16 12:00:00-03:00",
  "user_identifiers": [
    {"email": "user@example.com"},
    {"phone": "+15555550100"}
  ],
  "consent": {"ad_user_data": "GRANTED", "ad_personalization": "GRANTED"}
}
```

### Call conversion

```json
{
  "conversion_action_id": "7693341777",
  "caller_id": "+15555550100",
  "call_start_date_time": "2026-08-16 11:00:00",
  "conversion_date_time": "2026-08-16 12:00:00",
  "conversion_value": 30.0,
  "currency_code": "USD",
  "consent": {"ad_user_data": "GRANTED", "ad_personalization": "GRANTED"}
}
```

### Adjustment

Retraction:
```json
{
  "conversion_action_id": "7693341777",
  "adjustment_date_time": "2026-08-16 15:00:00",
  "adjustment_type": "RETRACTION",
  "order_id": "ORDER-42"
}
```

Restatement (cambio de valor):
```json
{
  "conversion_action_id": "7693341777",
  "adjustment_date_time": "2026-08-16 15:00:00",
  "adjustment_type": "RESTATEMENT",
  "order_id": "ORDER-42",
  "restatement_value": {"adjusted_value": 150.0, "currency_code": "USD"}
}
```

Enhancement (upgrade a Enhanced Conversions con user data):
```json
{
  "conversion_action_id": "7693341777",
  "adjustment_date_time": "2026-08-16 15:00:00",
  "adjustment_type": "ENHANCEMENT",
  "order_id": "ORDER-42",
  "user_identifiers": [{"email": "user@example.com"}]
}
```

Alternativa a `order_id`: `gclid_date_time_pair = {"gclid": "...", "conversion_date_time": "..."}`.

## Flujo típico

1. Preparar el batch (típicamente desde el CRM: gclid capturado en el lead + valor final del pipeline).
2. `upload_click_conversions(conversions=[...], validate_only=true, dry_run=true)` → aprobar con `confirmation_text=UPLOAD <N> RECORDS`.
3. Ejecutar con `dry_run=false, validate_only=false` → recibe `partial_failure_error` por errores individuales.
4. Repetir semanalmente. Los uploads acumulan en Google Ads con delay de 3-9 horas.
5. Si un valor cambia después (upsells, refunds), `upload_conversion_adjustments` con `RESTATEMENT` o `RETRACTION`.

## Errores frecuentes

- `TOO_RECENT_CONVERSION_DATE_TIME`: la conversión es muy reciente (los clicks tardan ~3h en indexarse).
- `INVALID_CONVERSION_DATE_TIME`: fuera del ventana `click_through_lookback_window_days` de la conversion action.
- `EMAIL_HASH_SIZE`: intentaste enviar email en claro donde se esperaba hashed. La tool hashea localmente, pero si el input es un email raro (con caracteres no imprimibles) puede fallar downstream.
- `CONSENT_TYPE_UNSPECIFIED`: nunca debería salir porque la tool ya lo bloquea local, pero indica que Google rechazó el consent.
