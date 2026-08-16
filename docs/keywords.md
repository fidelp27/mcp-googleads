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

## Waste intelligence — clustering de search terms

Dos tools sobre `search_term_view` que reemplazan el review manual de listas planas:

- **`analyze_search_terms_clusters(customer_id, days=30, min_cost_micros=0, similarity_threshold=0.5, campaign_id?)`** — agrupa términos sin conversiones por similitud de tokens (Jaccard). Devuelve clusters ordenados por gasto malgastado descendente, con `shared_tokens`, `top_terms` (top 5 por costo) y `totals`.
- **`suggest_negatives_from_clusters(customer_id, days=30, match_type="PHRASE", max_clusters=25, ...)`** — para cada cluster top, devuelve un `keyword_text` (los tokens compartidos, o el término de mayor costo como fallback) listo para pasar a `add_negative_keywords_bulk`.

### Tokenización

- Lowercase, split por regex `[a-z0-9áéíóúñü]+`.
- Filtro de stopwords ES+EN comunes (`a`, `de`, `el`, `for`, `to`, etc.).
- Tokens de menos de 2 caracteres se descartan.
- Cap de **2000 términos** por análisis (los más caros primero).

### Cuándo subir/bajar `similarity_threshold`

- **0.7-0.9** — clusters muy conservadores; sólo agrupa términos casi idénticos. Usar para revisiones donde vas a autorizar cada negativa manualmente.
- **0.5** (default) — balance entre coverage y precisión.
- **0.3-0.4** — clusters agresivos; agrupa por menos tokens compartidos. Usar cuando la cuenta tiene mucha variabilidad de long-tail y querés reducir el número de decisiones.

### Flujo

1. `analyze_search_terms_clusters(days=30, min_cost_micros=100000)` — filtrar por al menos 10 centavos de gasto por término para ignorar cola larga.
2. Revisar los top clusters (más gasto = más impacto de negativizarlos).
3. `suggest_negatives_from_clusters(match_type="PHRASE", max_clusters=10)` — pasar los top 10 clusters a modo suggestion.
4. Revisar cada `keyword_text` sugerida (¿tiene sentido? ¿coincide con búsquedas de marca?).
5. `add_negative_keywords_bulk(..., dry_run=true)` con la lista filtrada → aprobar → ejecutar.

**Nota:** las tools nunca aplican negativas por sí solas. `suggest_negatives_from_clusters` es lectura pura.
