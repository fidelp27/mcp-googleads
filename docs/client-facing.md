# Cómo operamos su cuenta con asistencia de IA

La agencia utiliza un asistente conectado a Google Ads para acelerar análisis, reporting y tareas operativas. El asistente no recibe libertad irrestricta sobre la cuenta.

## Qué puede hacer

- Consultar rendimiento y detectar oportunidades.
- Preparar cambios de campañas, anuncios, keywords, presupuestos y assets.
- Generar reportes y planes de optimización.
- Ejecutar solamente los cambios autorizados por la política acordada.

## Controles

- Cada cliente está identificado por una whitelist.
- Todo cambio se valida primero sin ejecutarse.
- Los cambios sensibles exigen una confirmación exacta.
- Podemos exigir aprobación del cliente antes de aplicar una mutation.
- Existen límites rígidos para presupuestos y bids.
- Cada operación queda asociada a un operador, hora, cliente y resultado.
- Tokens, claves y datos personales se redactan del log.

## Datos personales

Customer Match solo se habilita cuando existe consentimiento y base legal suficiente. La creación de listas exige una declaración explícita del operador. Las claves y archivos con datos no se almacenan en el repositorio.

## Incidentes

Ante una disputa, la agencia puede consultar el log append-only. Algunas operaciones reversibles pueden restaurarse; las eliminaciones y determinadas acciones de Google Ads pueden no ser reversibles.
