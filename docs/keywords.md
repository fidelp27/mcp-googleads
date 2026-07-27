# Runbook de keywords y waste

## Secuencia recomendada

1. `get_search_terms_report`.
2. `get_keyword_performance`.
3. `detect_waste`.
4. Revisión humana de intención y términos de marca.
5. `add_negative_keyword` o `add_negative_keywords_bulk` con dry-run.
6. Verificación posterior con el reporte de términos.

Nunca conviertas automáticamente todo término sin conversión en negativo. La ausencia de conversiones puede deberse a atribución incompleta, poco volumen o una ventana de conversión todavía abierta.

## Operaciones

- `add_keywords_to_ad_group`: EXACT, PHRASE o BROAD; permite CPC individual.
- `update_keyword_bid`: aplica `MAX_BID_MICROS` y pide confirmación si supera 3x el bid del ad group.
- `pause_keyword` / `enable_keyword`: reversibles.
- `remove_keyword`: destructiva y confirmada.
- `create_negative_keyword_list`: crea un shared set y miembros.
- `attach_negative_keyword_list_to_campaign`: conecta una lista existente.

## Prompt típico

> Analiza 30 días de términos de búsqueda, separa marca, competencia e irrelevantes, y prepara un dry-run de negativas exactas. No ejecutes cambios.
