# Diseño operativo

## Objetivo

`google-ads-mcp` permite que una agencia consulte y opere cuentas Google Ads bajo un MCC sin saltarse controles humanos. El servidor conserva el entrypoint `google_ads_server.py` y distribuye las capacidades de agencia dentro del paquete `agency`.

## Componentes

- `agency/api.py`: REST, paginación, retry, timeout y rate limiting.
- `agency/config.py`: entorno, whitelist, caps, contexto y configuración JSON.
- `agency/governance.py`: auditoría append-only, redacción e historial de approvals.
- `agency/mutations.py`: contrato uniforme validate-only, aprobación, idempotencia y ejecución.
- `agency/read_tools.py`: lectura complementaria.
- `agency/write_tools.py`: mutations por dominio.
- `agency/reports.py`: reporting, waste, alertas y optimización.
- `agency/mcc_tools.py`: contexto y consultas multi-cliente.
- `agency/governance_tools.py`: inspección, aprobación, rechazo y reversión.
- `agency/resources.py`: runbooks y prompts MCP embebidos.

## Invariantes

1. Toda mutation inicia con `dry_run=true`.
2. El preview se valida contra Google mediante `validateOnly=true`.
3. El `approval_id` queda ligado al operador, cliente y hash del payload.
4. Cambiar el payload invalida el approval.
5. `READ_ONLY_MODE=true`, `BLOCKED_TOOLS` y `ALLOWED_CUSTOMER_IDS` no pueden omitirse desde una tool.
6. Los caps económicos se validan antes de llamar a Google.
7. Las creaciones usan claves de idempotencia y resource names temporales cuando hay mutations dependientes.
8. Toda tool queda registrada en el log; secretos y datos de Customer Match se redactan.
9. Los reportes consultan datos frescos y no mantienen cache persistente.

## Estado de validación

La implementación fue completada sin ejecutar tests ni llamadas reales por decisión operativa. Antes de habilitar producción se deben validar los payloads con una cuenta Google Ads de prueba y luego ejecutar el checklist de compatibilidad MCP.
