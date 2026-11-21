# Piloto remoto de Google Ads MCP

El gateway presenta una URL MCP común, valida tokens OAuth y asigna cada identidad a un worker privado. Cada worker usa un perfil Google Ads distinto y conserva su configuración, aprobaciones y auditoría en directorios propios. Con `--allow-writes` carga el catálogo completo, incluidas las operaciones de escritura con los controles de aprobación y límites del servidor.

## Preparar el entorno

1. Instalá `python -m pip install ".[remote]"`. Ejecutá el gateway y los workers en el mismo host dedicado, con perfiles y directorios privados.
2. Configurá un emisor OAuth compatible con los clientes del piloto. El gateway espera un JWT firmado con RS256, un `sub` estable, audiencia igual a la URL pública de `/mcp` y el scope `mcp:access`. Configurá el descubrimiento, el registro de clientes y los redirects en ese emisor.
3. Prepará un perfil por usuario mediante el flujo existente de Google Ads `connect`. Cada perfil necesita las cuentas expresamente autorizadas por su titular. Para probar cambios, usá cuentas de prueba y permisos de Google Ads adecuados. Si dos usuarios operan la misma cuenta, sus estados de aprobación son independientes y los bloqueos locales de un worker no coordinan con el otro.
4. Compartí `MCP_REMOTE_WORKER_KEY` entre gateway y workers como secreto de despliegue. Generá un valor aleatorio de al menos 32 caracteres ASCII alfanuméricos, por ejemplo con `python -c "import secrets; print(secrets.token_hex(32))"`. Restringí el acceso remoto a los puertos de los workers.

## Arrancar las instancias

En procesos separados, con `OPERATOR_NAME` configurado para la identidad de cada worker:

```console
google-ads-mcp-server --profile /srv/profiles/user-a.json serve --worker-port 8101 --allow-writes
google-ads-mcp-server --profile /srv/profiles/user-b.json serve --worker-port 8102 --allow-writes
```

Configurá el gateway con estos valores de ejemplo:

| Variable | Valor |
| --- | --- |
| `MCP_REMOTE_RESOURCE_URL` | `https://ads-mcp.example.com/mcp` |
| `MCP_REMOTE_ISSUER` | `https://login.example.com` |
| `MCP_REMOTE_JWKS_URL` | URL HTTPS de claves públicas del emisor |
| `MCP_REMOTE_WORKERS` | `{"subject-a":"http://127.0.0.1:8101/mcp","subject-b":"http://127.0.0.1:8102/mcp"}` |
| `MCP_REMOTE_ALLOWED_ORIGINS` | Lista opcional de Origins confiables separados por espacios |
| `PORT` | Puerto local del gateway, `8080` por defecto |

Ejecutá `google-ads-mcp-server remote`. El proxy HTTPS debe reenviar `/mcp` y `/.well-known/oauth-protected-resource/mcp` al gateway, permitir streaming y validar el nombre de host público. El gateway valida la identidad y el recurso OAuth y reenvía la petición solo al worker asignado.

## Verificar antes de ampliar el piloto

- En Codex CLI y Claude Desktop, configurá la misma URL. Entrá como los dos usuarios y verificá listado de herramientas, informes y rechazo cuando se consulta una cuenta fuera del perfil propio.
- Con una cuenta de prueba, ejecutá un `dry_run` con una herramienta de escritura. Verificá que la aprobación emitida por un usuario no se pueda usar desde el worker del otro. Completá una mutación de prueba y confirmá su resultado y auditoría.
- Los argumentos de archivo en remoto apuntan al directorio `<perfil>-files` del worker. Para CSV y exportaciones, provisioná ese directorio en el host. Una carga y descarga desde el escritorio requiere una interfaz adicional.

El piloto usa un emisor OAuth externo y un mapa administrado de usuarios a workers. Para acceso público autogestionado por cada usuario faltan el flujo web de autorización de Google Ads, la persistencia cifrada de credenciales y el aprovisionamiento y revocación automáticos de perfiles y workers.
