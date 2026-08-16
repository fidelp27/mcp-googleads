# Playbook de agencia

## Onboarding de un cliente

1. Vincula la cuenta al MCC.
2. Agrega el ID a `ALLOWED_CUSTOMER_IDS` y `config/clients.json`.
3. Ejecuta `list_client_accounts`, `switch_context` y `get_client_context`.
4. Audita conversiones, moneda, campañas, presupuestos, search terms y cambios.
5. Configura umbrales en `config/alerts.json`.
6. Mantén `READ_ONLY_MODE=true` durante el diagnóstico.

## Reporte mensual

1. `generate_client_report(format="html")`.
2. `compare_periods`.
3. `detect_waste`.
4. `list_recommendations`.
5. `generate_optimization_plan`.
6. Separa acciones ejecutadas, propuestas y bloqueadas.

## Cambio grande

1. Reúne evidencia fresca.
2. Explica impacto, riesgo y rollback.
3. Ejecuta dry-run.
4. Comparte el preview con el cliente.
5. Registra la aprobación.
6. Ejecuta el mismo payload.
7. Verifica estado y conserva el `audit_id`.

## Bulk apply — cambios paralelos en múltiples clientes

`mcc_bulk_apply(operation_name, targets, dry_run, approval_id?, confirmation_text?)` aplica la misma operación en N clientes bajo el MCC con **una sola aprobación**. Escala real de agencia: pausar una campaña estacional en 20 clientes se resuelve en dos calls (dry-run + execute) en lugar de 40.

### Operaciones soportadas

| operation_name | payload por target | Uso típico |
|---|---|---|
| `pause_campaign` | `{"campaign_id": "..."}` | Cortar campañas Black Friday el 30/11. |
| `pause_ad` | `{"ad_group_id": "...", "ad_id": "..."}` | Retirar creativos con Ad Strength POOR. |
| `update_campaign_budget` | `{"budget_id": "...", "amount_micros": ...}` | Ajustar presupuestos según pacing. |

### Restricciones

- Máximo **50 targets** por call.
- No compatible con `CLIENT_APPROVAL_REQUIRED=true` (bulk asume una sola aprobación operativa; para cambios con approval cliente-side, ejecutá cada cliente por separado).
- Los caps `MAX_DAILY_BUDGET_INCREASE_MICROS` y `MAX_BUDGET_INCREASE_PCT` se aplican **por cliente** en `update_campaign_budget`. Un target que los viole aborta el bulk entero en dry-run.

### Flujo

1. **Dry-run** con `dry_run=true` y el dict `targets` — devuelve `approval_id`, `expected_confirmation="EXECUTE BULK <N>"`, un preview por cliente con `previous_state` capturado y errores por target si los hay.
2. **Revisar** cada preview y los `validation_errors`.
3. **Ejecución** con el mismo `targets`, `dry_run=false`, el `approval_id` y `confirmation_text="EXECUTE BULK <N>"`.
4. La ejecución **no aborta ante fallos por cliente**: cada resultado individual queda auditado (`mcc_bulk_apply.execute`) con su `audit_id` y el `operation_id=approval_id` del bulk. La respuesta consolida `executed_count` y `failed_count`.

### Ejemplo

```json
mcc_bulk_apply(
  operation_name="pause_campaign",
  targets={
    "5461642152": {"campaign_id": "24140931767"},
    "1234567890": {"campaign_id": "999"}
  },
  dry_run=true
)
```

Devuelve `approval_id` y `expected_confirmation="EXECUTE BULK 2"`. Después:

```json
mcc_bulk_apply(
  operation_name="pause_campaign",
  targets={...same targets...},
  dry_run=false,
  approval_id="9c2f689e-...",
  confirmation_text="EXECUTE BULK 2"
)
```

## Rutina diaria

- `generate_agency_dashboard`
- `check_alerts`
- `get_pending_approvals`
- `get_change_history` para cuentas con anomalías

Estas herramientas pueden invocarse desde n8n por cron, pero n8n no debe saltarse approvals ni caps.
