# Runbook de reporting y MCC

## Contexto multi-cliente

- `list_client_accounts` consulta la jerarquía del MCC.
- `switch_context` fija un cliente local, pero no cambia el header MCC.
- `get_client_context` combina estado de Google con `config/clients.json`.
- `run_across_clients` solo permite resúmenes de lectura definidos.

Incluye el MCC y los clientes operables en `ALLOWED_CUSTOMER_IDS`.

## Reporting

- `generate_client_report`: JSON, Markdown o HTML con branding.
- `generate_agency_dashboard`: métricas y approvals consolidados.
- `compare_periods`: variación absoluta y porcentual.
- `detect_waste`: evidencia priorizada, no cambios automáticos.
- `generate_optimization_plan`: payloads MCP sugeridos siempre en dry-run.
- `check_alerts`: evalúa los límites de `config/alerts.json`.

Los reportes no mantienen cache persistente. La moneda debe obtenerse de la cuenta y los costos continúan expresándose en micros dentro del detalle técnico.
