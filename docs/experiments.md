# Runbook de Drafts & Experiments

En Google Ads API v25 el flujo canónico usa `Experiment` + `ExperimentArm`
(la superficie legacy `CampaignDraft` está en camino a deprecación). Estas
tools cubren el loop diario de agencia: leer experiments existentes,
comparar métricas control vs treatment, crear/programar y terminar
experiments.

## Tools

| Tool | Rol | Uso |
|---|---|---|
| `list_experiments(customer_id, status?)` | Lectura | Todos los experiments del account con estado, fechas y promotion state. |
| `get_experiment(customer_id, experiment_id)` | Lectura | Un experiment con arms (control + treatment) y campañas asociadas. |
| `get_experiment_results(customer_id, experiment_id, days=30)` | Lectura | Comparación métrica control vs treatment (impressions, clicks, cost, conversions, CPA) con deltas en %. |
| `create_experiment(customer_id, name, base_campaign_id, suffix, traffic_split_percent, ...)` | Escritura | Crea un Experiment en SETUP + arm control apuntando a la base. El arm treatment se crea después. |
| `schedule_experiment(customer_id, experiment_id, validate_only_from_google=false, ...)` | Escritura | Arranca el long-running operation. |
| `end_experiment(customer_id, experiment_id, ...)` | Escritura destructiva | Halt de un experiment RUNNING. Devuelve tráfico a la campaña base. |

## Estados del experiment

`SETUP` → `INITIATED` → `RUNNING` → `HALTED` / `GRADUATED` / `PROMOTED`.

## Flujo típico

1. `create_experiment(base_campaign_id="123", name="Q4 bidding test", suffix="q4-test", traffic_split_percent=50, dry_run=true)`.
2. Revisar preview, ejecutar con `dry_run=false` + `approval_id` + `confirmation_text=name`.
3. Crear el ExperimentArm treatment desde la UI o vía `execute_gaql_query`/`mutate` custom (v1 no cubre creación de treatment arm porque requiere clonar la campaña base con modificaciones).
4. `schedule_experiment(experiment_id, dry_run=true)` → validar → `dry_run=false` → LRO arranca.
5. Esperar N días. `get_experiment_results(experiment_id, days=N)` para leer comparación.
6. Si el treatment gana, promocionar desde la UI (o `graduate_experiment` — no cubierto en v1 porque requiere mapping de presupuestos por campaña).
7. Si no gana o hay problema, `end_experiment(experiment_id, dry_run=true)` → `dry_run=false` con `confirmation_text="END <id>"`.

## Confirmation texts

- `create_experiment`: el `name` del experiment.
- `schedule_experiment`: `SCHEDULE <experiment_id>`.
- `end_experiment`: `END <experiment_id>`.

## Restricciones de v1

- `create_experiment` sólo crea el Experiment + control arm. Para el treatment arm hay que clonar la campaña base con las modificaciones deseadas (usar `copy_campaign` + editar) y luego crear el ExperimentArm via API custom o UI. El motivo: cada agencia decide qué modificar (bidding, keywords, ads), y no es trivial exponer eso genéricamente.
- `graduate_experiment` (promover treatment a campaña permanente) requiere mapping de presupuesto por campaña, quedó fuera de v1.
- Sólo `SEARCH_CUSTOM` type. Display / Video experiments quedan para follow-up.

## Errores frecuentes

- `EXPERIMENT_TYPE_ERROR: EXPERIMENT_ARM_MISSING`: intentaste programar un experiment sin ambos arms (control + treatment).
- `EXPERIMENT_CAMPAIGNS_ALREADY_EXPERIMENT`: la campaña base ya está en otro experiment activo.
- Suffix > 34 chars: la tool valida esto localmente antes de mandar la request.
