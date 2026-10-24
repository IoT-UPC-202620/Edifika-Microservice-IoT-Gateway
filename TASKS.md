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

## Task 3 — Cableado: eventos por AMQP, sync por HTTP con API key   [ ] pendiente
- Rama: feature/H1-event-sink-wiring
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: —

## Task 4 — Nodos y reservas desde la nube, con migración de la base   [ ] pendiente
- Rama: feature/H1-cloud-devices-reservations
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: —

## Task 5 — Decisión por rol y cambio de rol en el nodo   [ ] pendiente
- Rama: feature/H1-door-roles
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
- Bloqueos / notas: —

## Task 6 — Órdenes de la nube (sync-requested, rgb)   [ ] pendiente
- Rama: feature/H1-cloud-orders
- Paso actual: —
- Archivos tocados: —
- Tests: —
- Último commit: —
- Métricas: —
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
