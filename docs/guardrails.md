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

## Protocolo de mutation

1. La tool arma el payload y aplica validadores locales.
2. Google recibe el payload con `validateOnly=true`.
3. El servidor almacena hash, operador, cliente, estado previo y expiración.
4. Se devuelve un `approval_id` y, para alto riesgo, el texto exacto requerido.
5. La segunda llamada debe repetir argumentos y usar `dry_run=false`.
6. El hash impide ejecutar un payload distinto del aprobado.
7. La idempotency key evita duplicados ante reintentos.
8. Se devuelve un `audit_id`.

## Aprobación del cliente

Con `CLIENT_APPROVAL_REQUIRED=true`, el dry-run queda pendiente. Usa `approve_operation` con una referencia verificable —por ejemplo, ID de email, ticket o mensaje— y `consent_confirmed=true`. `reject_operation` cancela el cambio.

## Reversión

`revert_operation` soporta estados y presupuestos cuando el log contiene el estado anterior. No promete revertir eliminaciones, creación de recursos ni cambios que Google Ads considere irreversibles.

## Cambio de caps

Los caps no deben cambiarse durante una operación. Todo aumento debe quedar autorizado por el responsable de la agencia y documentado fuera del servidor.
