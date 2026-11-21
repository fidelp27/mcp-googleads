# Plan de ejecución para Google Ads MCP

El objetivo es que un equipo interno o una agencia pueda conectar sus propias cuentas, diagnosticar problemas, elegir cambios con evidencia y verificar su efecto desde un cliente MCP. La distribución inicial será open source, autogestionada y de solo lectura por defecto. Cada instalación usa su propio acceso a Google Ads. La compatibilidad se define por tipo de cuenta, canal, permisos y funciones disponibles en la API.

## Resultados que debe cubrir el producto

| Usuario | Trabajo que debe poder completar |
| --- | --- |
| Analista | Conectar una cuenta, comparar períodos y exportar gasto, resultados y valor con filtros reproducibles. |
| Especialista de campañas | Explicar por qué una campaña no entrega, preparar cambios de presupuesto, pujas, creatividades o exclusiones y verificar el resultado. |
| Responsable de medición | Saber qué eventos impulsan las pujas, comprobar cargas offline y localizar la fuente de discrepancias con web o CRM. |
| Líder de marketing | Revisar presupuesto, tendencias y alertas en moneda local y aprobar cambios de impacto. |
| Agencia | Alternar cuentas directas y bajo MCC, operar por lotes y mantener trazabilidad por cuenta. |

## Entregas y condiciones de salida

Estimación inicial de **12 a 16 semanas** para un desarrollador dedicado con apoyo semanal de un especialista de marketing y acceso a cuentas piloto. Las aprobaciones de Google, la disponibilidad de cuentas por vertical y la maduración de experimentos tienen plazos propios. Los bloques posteriores pueden solaparse con los pilotos.

| Entrega | Ventana | Trabajo | Condición de salida |
| --- | --- | --- | --- |
| 0. Instalación y base de pruebas | Semanas 1–2 | Elaborar una matriz de herramientas, versiones, cuenta directa/MCC, permisos y soporte por canal. Verificar rutas existentes. Empaquetar instalación local y Docker, incorporar guía OAuth, preflight y datos demo. Identificar errores de proyecto Cloud, MCC y permisos con pasos concretos. | Dos personas externas instalan sin ayuda, descubren sus cuentas y generan su primer reporte en hasta 30 minutos desde que tienen los permisos de Google. CI prueba instalación y arranque en Windows, macOS y Linux. |
| 1. Medición | Semanas 3–5 | Crear auditoría unificada de acciones, propietario de conversiones, objetivos estándar y personalizados, estrategia de puja y campañas afectadas. Agregar volumen y valor, latencia, diagnósticos offline y señales verificables de Enhanced Conversions. Corregir alertas actuales. Incorporar Data Manager para nuevas cargas y detectar integraciones heredadas elegibles. | En una cuenta piloto se puede explicar qué optimiza cada campaña y distinguir envío, aceptación y atribución. Una carga por lote en el entorno autorizado permite rastrear errores y pendientes sin registrar datos personales. |
| 2. Diagnóstico y operación diaria | Semanas 6–8 | Reunir estados, políticas, presupuesto, pujas, segmentación y URLs para explicar falta de entrega. Comparar períodos completos, usar zona horaria y moneda de la cuenta, distinguir fecha de interacción y conversión, contemplar retraso y tamaño de muestra. Unificar reportes, alertas ejecutables a demanda o por CLI y cambios frecuentes de Search. Agregar previsualización de cambios de objetivos y `primary_for_goal`. | Un especialista explica un problema de entrega y una caída de rendimiento con evidencia. Ejecuta un cambio aprobado, verifica el estado final y puede revertirlo cuando Google y el estado anterior lo permiten. |
| 3. Canales y experimentos | Semanas 9–12 | Completar PMax y Shopping con productos, listing groups, grupos de recursos, señales y reportes por canal según disponibilidad. Profundizar Display y Demand Gen con creatividades, rendimiento y exclusiones según canal. Completar experimentos Search con variante, programación, resultados estadísticos y promoción cuando corresponda. Añadir operaciones masivas con progreso, idempotencia y recuperación parcial. | Un ecommerce detecta un problema de producto o feed y puede preparar una corrección soportada. Un equipo de adquisición completa la configuración de un experimento y obtiene estados claros hasta que hay resultados interpretables. Una ejecución parcial reintenta solo los elementos pendientes o fallidos que admiten reintento. |
| 4. Pilotos y publicación | Semanas 13–16 | Probar equipos de leads, ecommerce y agencia MCC. Completar guía por rol, ejemplos en español e inglés, referencia de compatibilidad, demo reproducible, política de versiones y publicación en GitHub con un paquete instalable. Revisar el historial y artefactos públicos por credenciales y datos de clientes. | Los tres pilotos completan sus recorridos principales. Las diferencias con la interfaz de Google quedan explicadas. El proyecto pasa revisión de acceso a cuentas y pruebas de actualización desde la versión previa. |

## Backlog inicial

| Orden | Tarea | Evidencia para cerrarla |
| --- | --- | --- |
| 1 | Inventariar el catálogo del repositorio y del servidor en ejecución. Clasificar cada capacidad como dedicada, disponible por GAQL, dependiente de otra API o manual. | Matriz verificable con versión, requisitos, límites y enlace a la prueba correspondiente. |
| 2 | Crear preflight e instalación guiada para una cuenta directa y otra mediante MCC. | El flujo diferencia credenciales inválidas, cuenta fuera de alcance, nivel de proyecto insuficiente y falta de datos. |
| 3 | Construir la auditoría de objetivos de conversión y corregir alertas existentes. | casos de objetivo estándar, personalizado con secundaria y medición centralizada por MCC. |
| 4 | Incorporar diagnósticos de importación y una prueba de Data Manager. | Última carga, estados, errores y conciliación con el origen mediante IDs internos sin datos personales. |
| 5 | Crear el diagnóstico de entrega y actualizar detección de gasto sin resultados. | El informe separa causas confirmadas, hipótesis y datos insuficientes. Las conversiones recientes aparecen como provisionales. |
| 6 | Unificar previsualización, aprobación y verificación en las escrituras prioritarias. | Se rechazan cambios fuera de cuenta o presupuesto. Una aprobación obsoleta no se ejecuta si cambió el estado relevante. |

## Reglas para validar la versión pública

- **Lecturas.** Usar cuentas de prueba para permisos y contratos de API, datos sintéticos para regresiones y cuentas piloto con acceso autorizado para validar métricas reales. Comparar contra Google Ads con misma moneda, zona horaria, rango, columnas y filtros. Las cuentas de prueba no generan datos de entrega. [Pruebas en Google Ads](https://developers.google.com/google-ads/api/docs/best-practices/testing)
- **Escrituras.** Mostrar cuenta, recurso, cambio esperado, efecto sobre gasto y validación disponible. Usar `validateOnly` cuando la operación lo soporte y declarar la cobertura de la previsualización. Guardar actor, resultado y estado anterior cuando aplique. Para lotes, registrar resultados por elemento y verificar recuperaciones ante fallos parciales.
- **Datos.** En cada reporte incluir moneda, zona horaria, rango, momento de consulta y advertencias de cobertura. Mantener monedas separadas en el consolidado salvo que se use una conversión explícita. El cociente `conversions / all_conversions` se presenta como diferencia de medición, sin interpretarlo como prueba de consentimiento.
- **Acceso.** Probar roles de lectura y escritura, selección explícita de cuentas, dependencia del proyecto Cloud y configuración de MCC. La instalación local es el recorrido principal. Una instalación remota que exponga cuentas requiere autenticación y control de acceso al endpoint. [Niveles de acceso de Google Ads](https://developers.google.com/google-ads/api/docs/api-policy/access-levels)
- **Adopción.** Verificar con pilotos tareas completas, tiempo hasta primer reporte, errores de instalación y cambios aprobados con resultado verificable. Un fallo de medición que produzca recomendaciones erróneas, un cambio aplicado a otra cuenta o datos privados en el paquete bloquean la publicación.

## Cobertura y extensiones

- **Versión pública general.** Search, PMax, Shopping, Display y Demand Gen con una matriz por canal. La matriz distingue análisis, creación, edición y exclusiones. Los casos con acceso insuficiente, ausencia de datos o funciones no disponibles reciben estados diferenciados y un paso alternativo verificable.
- **Equipos especializados.** Incorporar App, Video/YouTube y verticales como Hotel según casos reales, cuentas piloto y operaciones que expone Google. El soporte de reporting puede salir antes que el de escritura.
- **Medición externa.** Para saber qué dispara una etiqueta o qué campos envía el backend, integrar después GA4, GTM y una interfaz de conciliación con CRM. Priorizar un adaptador de archivos y un piloto en Zoho antes de agregar conectores específicos. Para feeds, ampliar con Merchant API cuando el diagnóstico requiera datos de Merchant Center.
- **Fuentes restringidas.** Auction Insights requiere un flujo alternativo como importación de CSV exportado de la interfaz. Los resultados observados desde Google Ads, las pruebas de etiquetas y los registros del CRM deben conservar su origen en el informe. [Métricas de Google Ads](https://developers.google.com/google-ads/api/reference/rpc/v26/Metrics)

Referencias de implementación: [objetivos por campaña](https://developers.google.com/google-ads/api/docs/conversions/goals/campaign-goals), [migración de cargas offline a Data Manager](https://developers.google.com/data-manager/api/devguides/events/google-ads/offline/upgrade), [reportes PMax](https://developers.google.com/google-ads/api/performance-max/reporting), [experimentos](https://developers.google.com/google-ads/api/docs/experiments/overview) y [MCP oficial de Google Ads](https://developers.google.com/google-ads/api/docs/developer-toolkit/mcp-server). El MCP oficial sirve de referencia de compatibilidad. El diferencial del proyecto se prueba en auditorías operativas, informes accionables y cambios con aprobación y verificación.
