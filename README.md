# PrintServer

Servicio Windows en Python para escuchar transacciones en tiempo real por WebSocket e imprimir el encabezado y los detalles en una impresora local.

## Archivos principales

- `print_server.py`: escucha WebSocket, formatea e imprime transacciones.
- `print_server_service.py`: wrapper para instalar y ejecutar como servicio Windows.
- `.env`: conectividad, credenciales, URL WebSocket, impresora principal y cantidad de copias.
- `settings.toml`: servicio, ajustes de WebSocket e impresion avanzada.
- `print_server.log`: log rotativo diario.

## Configuracion

1. Instalar dependencias:

```powershell
pip install -r requirements.txt
```

2. Completar `.env`:

- `API_BASE_URL`: host base, por ejemplo `https://api.grupobasaly.pe`.
- `COMPANY`: id de la compania.
- `RUC`: RUC del local.
- `API_AUTH_MODE`: `basic`, `bearer`, `token` o `none`.
- `API_USERNAME` y `API_PASSWORD`: credenciales basic para el WebSocket.
- `API_TOKEN`: token si se usa autenticacion bearer/token.
- `WS_TRANSACTIONS_URL`: canal WebSocket.
- `DEFAULT_PRINTER_NAME`: impresora local para todas las transacciones.
- `PRODUCTION_PRINT_COPIES`: cantidad de copias.

3. Ajustar `settings.toml`:

- `[websocket].event_types`: por defecto `transaction.created` y `transaction.updated`.
- `[websocket].log_payloads`: activar temporalmente con `true` para diagnosticar el JSON recibido.
- `[print]`: configuracion avanzada de formato, corte, beep y encoding.

## Ejecucion manual

```powershell
python .\print_server.py
```

Listar impresoras:

```powershell
python .\print_server.py --list-printers
```

## Servicio Windows

Instalar o actualizar:

```powershell
powershell -ExecutionPolicy Bypass -File .\install_or_update_service.ps1
```

Iniciar:

```powershell
python .\print_server_service.py start
```

Detener:

```powershell
python .\print_server_service.py stop
```

Eliminar:

```powershell
python .\print_server_service.py remove
```

## Observaciones

- La impresion se activa solo por eventos WebSocket de transacciones.
- No se consulta ni se actualiza una API REST despues de imprimir.
- El cache local evita reimpresiones del mismo detalle mientras el servicio sigue encendido.
- Si necesitas diagnosticar payloads, activa `log_payloads = true` en `settings.toml` de forma temporal.
