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

## Rutina diaria

- `generate_agency_dashboard`
- `check_alerts`
- `get_pending_approvals`
- `get_change_history` para cuentas con anomalías

Estas herramientas pueden invocarse desde n8n por cron, pero n8n no debe saltarse approvals ni caps.
