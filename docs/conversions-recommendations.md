# Runbook de conversiones y recomendaciones

## Conversiones

Antes de optimizar bids, ejecuta `get_conversion_actions` y `get_conversion_performance`. Una cuenta con tracking incompleto no debe migrarse a estrategias automáticas por conversiones.

- `create_conversion_action`: crea una conversión web.
- `update_conversion_action`: modifica campos explícitamente permitidos.
- `pause_conversion_action`: la oculta para evitar que siga utilizándose operativamente.

Los cambios de conversiones requieren dry-run, confirmación y verificación posterior.

## Recomendaciones

1. `list_recommendations`.
2. `get_recommendation_detail`.
3. Evalúa impacto, supuestos y objetivos del cliente.
4. `apply_recommendation` con preview y confirmación exacta, o `dismiss_recommendation`.

Las recomendaciones de Google no son órdenes. Presupuesto, bidding y expansión de targeting pueden tener impacto financiero y deben pasar los mismos guardarraíles que un cambio manual.
