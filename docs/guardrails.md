# Guardarraíles y aprobaciones

## Controles activos

| Control | Configuración |
|---|---|
| Bloqueo global de escritura | `READ_ONLY_MODE` |
| Aislamiento de cuentas | `ALLOWED_CUSTOMER_IDS` |
| Bloqueo por rol | `BLOCKED_TOOLS` |
| Aumento absoluto de presupuesto | `MAX_DAILY_BUDGET_INCREASE_MICROS` |
| Aumento porcentual | `MAX_BUDGET_INCREASE_PCT` |
| Bid máximo | `MAX_BID_MICROS` |
| Cuota local | `GOOGLE_ADS_RATE_LIMIT_PER_MINUTE` |
| Vigencia de previews | `APPROVAL_TTL_MINUTES` |
| Aprobación cliente | `CLIENT_APPROVAL_REQUIRED` |
| Retención local de auditoría | `AUDIT_RETENTION_DAYS` |
| Timeout de resource lock | `LOCK_TIMEOUT_SECONDS` |

## Protocolo de mutation

1. La tool arma el payload y aplica validadores locales.
2. Google recibe el payload con `validateOnly=true`.
3. El servidor almacena hash, operador, cliente, estado previo, diff y expiración.
4. Se devuelve un `approval_id`, un `diff` legible, y para alto riesgo el texto exacto requerido.
5. La segunda llamada debe repetir argumentos y usar `dry_run=false`.
6. El hash impide ejecutar un payload distinto del aprobado.
7. La idempotency key evita duplicados ante reintentos.
8. El resource lock impide que dos operadores muten el mismo recurso a la vez.
9. Se devuelve un `audit_id` y, si hubo contención, `lock_wait_ms`.

## Preview de diff en updates

Toda tool de mutación que capture `previous_state` obtiene automáticamente un `diff` en la respuesta validated y en el audit entry. Formato por operación:

```json
{
  "resource": "customers/1234567890/campaignBudgets/22222222222",
  "update_mask": ["amount_micros"],
  "changes": [
    {"field": "amount_micros", "old": "3000000000", "new": "6000000000", "changed": true}
  ]
}
```

- El diff usa los `updateMask` de la operación; snake_case → camelCase se maneja internamente para leer `previous_state` (Google devuelve `amountMicros`).
- Valores numéricos string vs int se normalizan al comparar (`"5000000" == 5000000`) para no falsear cambios.
- `changed=false` aparece cuando el mask incluyó un campo que no varió — el operador ve exactamente qué campos fueron revisados.
- Sin `mask_fields` el diff sólo lista los campos cambiados a nivel top.

Helpers: `agency.governance.build_diff(before, after, mask_fields?)` y `build_diff_from_operations(previous_state, operations)`.

## Errores estructurados

Las llamadas a Google Ads levantan `agency.errors.GoogleAdsError` (hereda de `RuntimeError` para backward-compat). El envelope real (`error.details[].errors[]`) se parsea automáticamente:

```python
try:
    search(cid, query)
except GoogleAdsError as exc:
    if exc.has_code("authorizationError", "METRIC_ACCESS_DENIED"):
        return {"error": {...}, "details": exc.to_dict()}
    raise
```

Fields expuestos por `GoogleAdsErrorItem`:

- `error_code_category` (ej. `authorizationError`, `queryError`, `keywordPlanError`).
- `error_code_value` (ej. `METRIC_ACCESS_DENIED`, `UNRECOGNIZED_FIELD`).
- `message` — el mensaje humano de Google.
- `location_field` — path del campo (ej. `operations[0].create`), si Google lo entrega.
- `docs_url` — link directo al Enum path oficial (`.../reference/rpc/v25/{EnumPath}`).

Helpers:

- `GoogleAdsError.from_response(http_status, api_version, body)` — parsing tolerante (non-JSON, empty body, malformed details).
- `exc.has_code(category, value?)` — matching estructurado sin string comparisons frágiles.
- `exc.to_dict()` — serialización estable para audit y responses.

## Aprobación del cliente

Con `CLIENT_APPROVAL_REQUIRED=true`, el dry-run queda pendiente. Usa `approve_operation` con una referencia verificable —por ejemplo, ID de email, ticket o mensaje— y `consent_confirmed=true`. `reject_operation` cancela el cambio.

## Alertas de approvals que expiran

`list_expiring_approvals(minutes_until_expiry=15)` devuelve approvals pending cuyo `expires_at` cae dentro de la ventana, ordenados por urgencia (menor `seconds_until_expiry` primero). Los ya expirados quedan incluidos con `already_expired=true` para post-mortem.

Recomendación operativa: ejecutarlo desde un cron cada 5-10 min durante la ventana de trabajo, para no perder aprobaciones ya reviewed por vencimiento silencioso.

## Resource locking multi-operador

`agency.locks.resource_lock(customer_id, resource_key)` es un mutex de archivo (portable Windows/Unix vía `filelock`) que se toma automáticamente en `execute_mutation` a partir del `resourceName` de la operación.

Cuando dos operadores tocan el mismo recurso al mismo tiempo:

- El segundo espera hasta `LOCK_TIMEOUT_SECONDS` (default 30s).
- Si expira, se emite `ResourceLockedError` con el mensaje canónico `RESOURCE_LOCKED customer=… resource=… timeout=…s lock_path=…`.
- El audit graba un entry `lock_conflict` con el `resource_key` y el `timeout_seconds` — los conflictos son diagnosticables sin polling.
- Las ejecuciones exitosas devuelven `lock_wait_ms` para monitorear contención.

El lock es **por (customer_id, resource_name)** — dos operadores editando campañas distintas del mismo cliente no se bloquean entre sí; dos operadores editando la misma campaña sí. Los locks viven en `data/locks/{customer_id}/`.

## Reversión

`revert_operation` soporta estados y presupuestos cuando el log contiene el estado anterior. No promete revertir eliminaciones, creación de recursos ni cambios que Google Ads considere irreversibles.

## Auditoría — export y rotación

El audit log append-only se escribe en `data/audit.jsonl` + una copia diaria `data/audit-YYYY-MM-DD.jsonl`. Redacción automática de fields sensibles (passwords, tokens, `members`, `user_identifiers`, `conversions`, `adjustments`, `caller_id`).

**`export_audit_log(destination, format, days?, operator?, customer_id?, tool_name?, status?, since?, until?)`**

Extrae entradas filtradas a un archivo local:

- `format=jsonl` — preserva nesting completo.
- `format=csv` — columnas planas; nested se serializa como JSON string.
- `format=markdown_report` — agrupado por día, formato humano para review.

Filtros: `days`, `operator`, `customer_id`, `tool_name`, `status` (`executed` / `validated` / `error` / `lock_conflict` / etc.), o rangos ISO (`since`/`until`). Escribe con `chmod 0600` best-effort.

**`rotate_audit_log(retention_days?)`**

Mueve archivos `audit-YYYY-MM-DD.jsonl` más viejos que `retention_days` a `audit-archive-YYYY-MM.zip` mensuales (append-mode — nada se pierde entre corridas), y borra los sueltos. El `audit.jsonl` principal y cualquier otro archivo del directorio no se tocan. Default de `retention_days` es `AUDIT_RETENTION_DAYS` de settings.

Flujo recomendado:

1. `export_audit_log(destination="exports/audit-2026-Q3.md", format="markdown_report", since="2026-07-01T00:00:00Z", until="2026-09-30T23:59:59Z")` — mensual para clientes que piden reporting.
2. `rotate_audit_log()` — mensual o trimestral para mantener el working directory manejable.

## Cambio de caps

Los caps no deben cambiarse durante una operación. Todo aumento debe quedar autorizado por el responsable de la agencia y documentado fuera del servidor.
