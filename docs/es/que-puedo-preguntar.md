# ¿Qué puedo preguntarle? — Prompts de ejemplo por alcance y nivel

Guía en lenguaje simple de **qué podés pedirle al MCP** desde tu cliente de IA
(Claude Desktop, Claude Code, Codex, Cursor, Gemini CLI, etc.) una vez
conectado a una cuenta de Google Ads.

No hace falta ser experto en marketing. Si podés describir qué querés saber
("¿está roto algo?", "¿a dónde se me va la plata?", "¿voy muy rápido con el
presupuesto?"), el MCP se encarga de elegir las tools correctas.

Cada sección corresponde a un **preset** (`core`, `leads`, `ecommerce`,
`agency`, `full`) — el paquete de tools que se carga. Elegí el más parecido
a tu caso; después podés cambiar (ver el [README](../../README.md#3-tool-catalog--presets---preset----modules)).

---

## Preguntas de nivel general (funcionan en cualquier preset)

Son los prompts de "solo quiero entender qué está pasando". Funcionan
incluso en el preset `core` (el default).

**Estado y cuentas**
- *"¿Está funcionando el servidor? ¿A qué cuenta estoy conectado?"*
- *"Listame las cuentas de Google Ads que puedo ver."*
- *"¿En qué moneda y zona horaria está esta cuenta?"*

**Panorama general**
- *"¿Cómo rindió la cuenta el mes pasado? ¿Y los últimos 7 días?"*
- *"Mostrame las 10 campañas que más gastaron y cuáles son rentables."*
- *"Comparame este mes contra el anterior — ¿qué cambió?"*
- *"Dame la tendencia diaria de gasto y conversiones de los últimos 30 días."*
- *"Armame un reporte de cliente en Markdown de los últimos 30 días."*

**Dónde estás tirando plata**
- *"¿Dónde estoy perdiendo plata?"*
- *"¿Qué términos de búsqueda gastaron sin convertir?"*
- *"¿Qué keywords tienen alto costo y cero conversiones?"*
- *"¿Hay campañas limitadas por presupuesto?"*

**Alertas y sugerencias**
- *"Chequeá las alertas — ¿algo pasado de presupuesto, CPA o desperdicio?"*
- *"Dame un plan de optimización — ¿qué arreglo primero?"*

---

## Preset `core` — Default (25 tools)

**Para quién:** cualquiera que arranca. Vista general de la cuenta,
reportes, tendencias, detección de desperdicio.

**Qué podés preguntar:**

| Pregunta | Qué hace el MCP |
| --- | --- |
| *"¿Cuánto gasté la semana pasada vs. la anterior?"* | Comparación de períodos con % de delta. |
| *"Mostrame CTR, CPC, CPA y ROAS por campaña."* | Tabla de performance por campaña. |
| *"¿Qué creatividades de anuncios rinden mal?"* | Headlines, descriptions, ad strength y aprobaciones de las RSA. |
| *"¿Falta alguna imagen o hay assets rechazados?"* | Inventario de image assets y su uso. |
| *"Armame el reporte mensual del cliente en HTML."* | Reporte ejecutivo con branding, deltas y top items. |
| *"¿Cómo está la utilización de presupuesto por campaña?"* | Gasto vs. presupuesto configurado, marca las campañas topeadas. |
| *"¿Quién cambió qué la semana pasada?"* | Lee el change history de Google Ads. |
| *"¿Qué estrategia de puja tiene cada campaña?"* | Lista de portfolio y estrategias por campaña. |
| *"Corré esta GAQL personalizada: ..."* | Ejecuta GAQL de forma segura con límites de filas. |

---

## Preset `leads` — Generación de leads (54 tools)

**Para quién:** B2B, servicios, cualquiera que capta leads por formulario,
llamada o registro.

**Suma** diagnostics, measurement (tracking de conversiones) y search.

**Qué podés preguntar:**

- *"¿Por qué no se muestran mis anuncios?"* → diagnóstico 360° de delivery
  (billing, presupuesto, políticas, pérdidas de impression share).
- *"¿Voy a llegar al presupuesto mensual?"* → pacing MTD con proyección de
  fin de mes.
- *"Auditá mi tracking de conversiones — ¿hay algo roto o duplicado?"*
- *"¿Está funcionando Enhanced Conversions for Leads?"*
- *"¿Mis subidas de conversiones offline están entrando?"*
- *"Mostrame los peores search terms por gasto."*
- *"Agrupá los search terms en clusters y sugerime negativas."*
- *"¿Cómo está distribuido el Quality Score en mis keywords?"*
- *"¿Alguna Responsive Search Ad está en POOR o AVERAGE?"*
- *"¿Estoy perdiendo impression share por presupuesto o por rank?"*
- *"¿Qué landing pages tienen la peor experiencia mobile?"*
- *"¿Cómo rinde cada segmento demográfico (edad, género, ingreso)?"*
- *"Dame ideas de keywords para 'clínica dental Madrid'."*
- *"¿Qué volumen histórico y competencia tienen estas keywords?"*

---

## Preset `ecommerce` — Tiendas online (47 tools)

**Para quién:** Shopify, WooCommerce, PrestaShop, o cualquiera que venda
productos online con Performance Max y Shopping.

**Suma** diagnostics, measurement, PMax y Shopping.

**Qué podés preguntar:**

- *"Corré una auditoría completa de mis campañas Performance Max."*
- *"¿Qué asset groups de PMax están débiles o con assets rechazados?"*
- *"¿Qué search terms están activando mis campañas PMax?"*
- *"¿PMax se está mostrando en apps mobile que no quiero?"*
- *"¿Qué SKUs están perdiendo plata?"*
- *"Mostrame ROAS por marca, por categoría y por producto."*
- *"¿Qué productos son ganadores y cuáles son gasto desperdiciado?"*
- *"¿Cómo rinde por dispositivo, por geo, por hora del día?"*
- *"¿Qué recomendaciones de Google Ads están pendientes?"*
- *"Auditá mi tracking — ¿el valor de la compra está entrando bien?"*

---

## Preset `agency` — Multi-cuenta / MCC (71 tools)

**Para quién:** agencias, consultores y equipos in-house que manejan varias
cuentas bajo un Manager Account (MCC).

**Suma** diagnostics, measurement, search, PMax y orquestación MCC.

**Qué podés preguntar:**

- *"Listame todas las cuentas cliente debajo de mi MCC."*
- *"Armame un dashboard de agencia con todos los clientes — separado por moneda."*
- *"Corré esta GAQL en paralelo sobre todas las cuentas cliente."*
- *"Cambiá al contexto del cliente X y auditá su cuenta."*
- *"¿Qué clientes necesitan review semanal esta semana?"*
- *"Mostrame las aprobaciones pendientes en todas las cuentas."*
- *"¿Qué aprobaciones están por vencerse?"*
- *"Exportame el audit log en CSV para compliance."*
- *"Cargá el profile `agency-weekly` y corrélo sobre el cliente Y."*

---

## Preset `full` — Usuario avanzado (74 tools de lectura, o 142 con writes)

**Para quién:** usuarios técnicos que quieren todo disponible. Habilita
Display, Demand Gen, Experiments y (opcional) las 68 tools de escritura
con guardarraíles.

**Suma** Display, Demand Gen y (opt-in) el módulo `operations`.

**Extras de solo lectura:**

- *"Auditá mis placements de Display — ¿cuáles están tirando plata en apps mobile?"*
- *"¿Cuál es mi view rate y el embudo de cuartiles en Demand Gen?"*
- *"¿Qué placements tienen cero conversiones?"*
- *"Listame los experiments activos y compará control vs. tratamiento."*

**Con escrituras habilitadas** (`READ_ONLY_MODE=false` + aprobación humana
explícita en cada cambio):

- *"Agregá estas 15 keywords negativas a la campaña X."* → genera preview en
  dry-run y espera tu confirmación.
- *"Pausá las campañas con CPA mayor a $200 y cero conversiones."* →
  dry-run primero, ejecuta solo si confirmás.
- *"Subí 20% el presupuesto de la campaña Y."* → respeta los caps económicos
  (`MAX_DAILY_BUDGET_INCREASE_MICROS`, `MAX_BUDGET_INCREASE_PCT`).
- *"Subí estas conversiones offline desde mi CRM."*
- *"Revertí la última pausa de campaña que aprobé."*

Cada escritura pasa por: **preview en dry_run → approval_id → frase exacta
de confirmación → ejecución**. Nada se cambia sin tu visto bueno.

---

## Analysis profiles — auditorías guiadas

En vez de encadenar prompts, cargá un **profile** y dejá que el MCP corra
una auditoría estructurada:

- *"Cargá el profile `leads-audit` y auditá la cuenta 1234567890."*
- *"Cargá el profile `ecommerce-audit` sobre esta cuenta."*
- *"Cargá el profile `agency-weekly` para mi MCC."*

Los profiles son archivos Markdown + TOML que vienen con el servidor.
Podés escribir los tuyos en `GOOGLE_ADS_CONFIG_DIR/profiles/`.

---

## Probarlo sin credenciales (`--demo`)

¿Todavía no querés conectar una cuenta real? Registrá con `--demo` y todo
lo de arriba funciona contra dos cuentas sintéticas:

- `1111111111` — ecommerce en EUR (Europe/Madrid)
- `2222222222` — leads B2B en USD (America/New_York)

```bash
google-ads-mcp-server register --client claude-desktop --demo
```

Después hacé cualquiera de las preguntas de arriba y vas a ver output real
de las tools con datos ficticios.

---

## ¿Seguís sin saber por dónde empezar?

Un buen prompt inicial en cualquier preset:

> *"Auditá esta cuenta de Google Ads y decime las 3 cosas más importantes
> que debería arreglar, por orden de impacto."*

El MCP decide qué tools llamar, marca qué sabe con certeza vs. qué necesita
más datos, y te devuelve una lista priorizada.
