# TASKS — Lane B (Plan 1: Edge Gateway)

## Task 1 — Configuración, TLS MQTT y broker independiente   [x] hecha
- Rama: feature/H1-configurable-endpoints
- Paso actual: Step 6 — Commit and merge to develop
- Archivos tocados: .env.example, docker-compose.yml, edge_gateway/config.py, edge_gateway/gateway.py, edge_gateway/mqtt/bridge.py, edge_gateway/services/cloud.py, requirements.txt, tests/test_cloud_integration.py, tests/test_config.py, tests/test_mqtt_contract.py, TASKS.md
- Tests: .venv/Scripts/python -m pytest -q → 193 passed
- Último commit: feat(config): configurable MQTT TLS, AMQP and sync endpoints with legacy aliases (H1)
- Métricas: 193 tests passing
- Bloqueos / notas: —

## Task 2 — Publicador AMQP de eventos con confirmaciones   [x] hecha
- Rama: feature/H1-amqp-publisher
- Paso actual: Step 5 — Commit and merge to develop
- Archivos tocados: edge_gateway/services/transport.py, edge_gateway/services/amqp.py, edge_gateway/services/cloud.py, edge_gateway/services/outbox.py, edge_gateway/services/sync.py, tests/helpers.py, tests/test_amqp_publisher.py, TASKS.md
- Tests: .venv/Scripts/python -m pytest -q → 200 passed, 6 skipped
- Último commit: feat(amqp): publish outbox events to RabbitMQ with publisher confirms (H1)
- Métricas: 200 passed in < 15s
- Bloqueos / notas: 6 contract tests skipped until Platform schemas published by Lane A.

## Task 3 — Cableado: eventos por AMQP, sync por HTTP con API key   [x] hecha
- Rama: feature/H1-event-sink-wiring
- Paso actual: Step 5 — Commit and merge to develop
- Archivos tocados: edge_gateway/__init__.py, edge_gateway/gateway.py, edge_gateway/services/cloud.py, tests/test_cloud_integration.py, tests/test_gateway_wiring.py, TASKS.md
- Tests: .venv/Scripts/python -m pytest -q → 202 passed, 6 skipped
- Último commit: refactor(gateway): split event sink (AMQP) from cache sync client (HTTP) (H1)
- Métricas: 202 passed in < 15s
- Bloqueos / notas: —

## Task 4 — Nodos y reservas desde la nube, con migración de la base   [x] hecha
- Rama: feature/H1-cloud-devices-reservations
- Paso actual: Step 5 — Commit and merge to develop
- Archivos tocados: edge_gateway/models.py, edge_gateway/db.py, edge_gateway/services/devices.py, edge_gateway/services/sync.py, tests/test_persistence.py, tests/test_sync_devices.py, TASKS.md
- Tests: .venv/Scripts/python -m pytest -q → 209 passed, 6 skipped
- Último commit: feat(sync): cloud-managed devices and door reservations with schema migration (H1)
- Métricas: 209 passed in < 15s
- Bloqueos / notas: —

## Task 5 — Decisión por rol y cambio de rol en el nodo   [x] hecha
- Rama: feature/H1-door-roles
- Paso actual: Step 5 — Commit and merge to develop
- Archivos tocados: edge_gateway/services/access.py, edge_gateway/gateway.py, tests/test_access_modes.py, TASKS.md
- Tests: .venv/Scripts/python -m pytest -q → 214 passed, 6 skipped
- Último commit: feat(access): door role from reservations with offline mode switching (H1)
- Métricas: 214 passed in < 15s
- Bloqueos / notas: —

## Task 6 — Órdenes de la nube (sync-requested, rgb)   [x] hecha
- Rama: feature/H1-cloud-orders
- Paso actual: Step 5 — Commit and merge to develop
- Archivos tocados: edge_gateway/services/cloud_commands.py, edge_gateway/services/amqp.py, edge_gateway/services/commands.py, edge_gateway/mqtt/contract.py, edge_gateway/gateway.py, tests/test_cloud_commands.py, tests/test_amqp_publisher.py, TASKS.md
- Tests: .venv/Scripts/python -m pytest -q → 231 passed (0 skipped)
- Último commit: feat(amqp): consume cloud orders (sync-requested, rgb) from the gateway queue (H1)
- Métricas: 231 passed in < 15s (including Platform JSON schema contract tests)
- Bloqueos / notas: —

## Task 7 — Temperatura y eventos de estado del nodo   [ ] pendiente
- Rama: feature/H1-temperature-device-status
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: —

## Task 7A — Dialecto del firmware de referencia (esp32) y recuperación del rol   [ ] pendiente
- Rama: feature/H1-esp32-firmware-dialect
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: —

## Task 8 — Simulador del nodo de puerta   [ ] pendiente
- Rama: feature/H1-door-simulator
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: —

## Task 9 — Integración real con RabbitMQ y Mosquitto (métricas H1)   [ ] pendiente
- Rama: feature/H1-rabbitmq-integration-tests
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: Requiere notificación previa al coordinador ("B READY FOR TASK 9") y su explicit "go".

## Task 10 — Documentación y cierre de H1   [ ] pendiente
- Rama: feature/H1-docs
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: —
