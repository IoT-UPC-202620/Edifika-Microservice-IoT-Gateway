# Edifika Edge Gateway

On-premise gateway of the Edifika smart-building platform. It sits between the **ESP32 nodes**
(RFID reader, electric lock, OLED, buzzer, RGB LED, humidity, ultrasonic and temperature sensors)
and the **cloud backend**, and it keeps working when the internet is down.

```
 ESP32 nodes ──MQTT (v1)──▶  Mosquitto  ──▶  Edge Gateway  ──AMQP (RabbitMQ)──▶  Cloud Platform
 (sensors, RFID,            (local          Flask · Peewee     edifika.events            (Access & Telemetry)
  lock, OLED, buzzer,        broker)        SQLite · pika     edifika.edge.commands
  RGB LED)                                        │
                                                  └──HTTP Sync──▶  IoT Access Management
                                                    (Bearer API Key)  (cache sync pull)
```

What it does:

- **Decides access locally.** A card read is resolved from a cached copy of credentials, reservations
  and area schedules, evaluating door mode (`FREE_ACCESS` vs `RESERVED`) completely offline.
- **Answers the node** with what to do: unlock for N ms, message for the OLED, buzzer pattern, and LED color.
- **Validates sensor readings** (`humidity`, `ultrasonic`, `temperature`), converts ultrasonic distance into a water-level percentage, and raises
  local alerts (low humidity, critical water level, sensor fault) once each, with hysteresis.
- **Transports events to RabbitMQ** through a durable outbox with publisher confirms to `edifika.events`:
  batches, retries with capped backoff, idempotent event ids, chronological order, nothing lost during an outage.
- **Consumes cloud orders** from `edge.<gateway_id>.commands` bound to `edifika.edge.commands` (e.g. `SyncRequested` and `DeviceCommandRequested` for `rgb`).
- **Keeps the cache in sync** with IoT Access Management via HTTP pull with API key (`GET /api/v1/edge/sync`) and schema migration.
- **Tracks the nodes**: registry, heartbeat, offline detection, clock synchronisation, door mode recovery on reconnect,
  remote commands with acknowledgement, and a maintenance mode that fully deactivates a node.

Stack: Python 3.12+, Flask 3.1, Peewee ORM (+ playhouse migrator), SQLite (WAL mode), paho-mqtt, pika (AMQP), marshmallow.

## Run the stack (Docker Compose)

```bash
docker compose --profile broker up -d                  # Mosquitto MQTT broker
docker compose --profile cloud-local up -d             # RabbitMQ 4 management broker
docker compose up --build                              # Edge Gateway + mock backend
docker compose --profile sim up --build                # ... plus virtual ESP32 nodes
```

| Service | Port | Profile | Purpose |
|---|---|---|---|
| `edge-gateway` | 8000 | (default) | REST API · Swagger UI at `http://localhost:8000/docs` · `/health` |
| `mosquitto` | 1883 | `broker` | Local MQTT broker |
| `rabbitmq` | 5672, 15672 | `cloud-local` | AMQP broker with management UI at `http://localhost:15672` |
| `mock-cloud` | 9000 | (default) | Fake backend for local testing |

Defaults work out of the box (`SERVICE_TOKEN=dev-service-token`); copy `.env.example` to `.env` to customize.

Try it with the simulator:

```bash
# present a card at the door simulator
python -m simulator tap --device esp32-acc-01 --uid 04A1B2C3

# run continuous heartbeat and temperature/humidity publishing
python -m simulator run --device esp32-acc-01 --interval 10
```

## Run locally (without Docker)

```bash
python -m venv .venv && .venv/Scripts/activate        # source .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt
pytest                                                 # unit & contract test suite (< 30s)

export EDGE_SERVICE_TOKEN=dev-service-token EDGE_MQTT_HOST=localhost
python main.py
```

## Running Integration Tests

Integration tests run real round-trip scenarios against real RabbitMQ and Mosquitto brokers:

1. Tap reaches RabbitMQ `edifika.events` in ≤ 6s.
2. 20 taps during network outage queued and delivered once with zero duplicates.
3. Cloud order (`DeviceCommandRequested`) consumed from RabbitMQ, sent over MQTT to node in < 3s, and `CommandResult` confirmed.
4. Local access decision delivered to node in < 500ms.

To run:

```bash
# Start broker containers or run docker compose from Edifika-IoT-Platform
$env:EDGE_IT_AMQP_URL = "amqp://edifika:edifika-dev@localhost:5672/"
$env:EDGE_IT_MQTT_HOST = "localhost"
python -m pytest -m integration -v
```

## Configuration

Environment variables (see `edge_gateway/config.py` for defaults):

| Variable | Default | Meaning |
|---|---|---|
| `EDGE_GATEWAY_ID` | `edge-gateway-01` | Gateway identity code |
| `EDGE_SERVICE_TOKEN` | — | Bearer token for local REST API (required) |
| `EDGE_DATABASE_PATH` | `data/edge-gateway.db` | SQLite database file |
| `EDGE_MQTT_HOST` / `_PORT` | `localhost` / `1883` | Local MQTT broker endpoint |
| `EDGE_MQTT_USERNAME` / `_PASSWORD` | empty | Broker credentials |
| `EDGE_MQTT_TLS` | `false` | Enable TLS for MQTT broker |
| `EDGE_MQTT_CA_FILE` | empty | CA certificate file for MQTT TLS |
| `EDGE_MQTT_CLIENT_ID` | empty | Custom MQTT client ID (defaults to gateway ID) |
| `EDGE_AMQP_URL` | empty | RabbitMQ connection URL |
| `EDGE_AMQP_EVENTS_EXCHANGE` | `edifika.events` | Topic exchange for published events |
| `EDGE_AMQP_COMMANDS_EXCHANGE` | `edifika.edge.commands` | Topic exchange for inbound cloud commands |
| `EDGE_AMQP_PREFETCH` | `10` | Consumer QoS prefetch limit |
| `EDGE_SYNC_BASE_URL` | empty | IoT Access Management base URL |
| `EDGE_SYNC_API_KEY` | empty | Bearer API key for cache sync |
| `EDGE_TIMEZONE` | `America/Lima` | Timezone for area schedules |

> [!NOTE]
> `EDGE_CLOUD_BASE_URL` and `EDGE_CLOUD_TOKEN` are deprecated legacy aliases for `EDGE_SYNC_BASE_URL` and `EDGE_SYNC_API_KEY`.

## MQTT Contract (schema version 1)

Prefix `edifika/v1`. Contract definition and JSON schemas follow `Edifika-IoT-Platform/contracts`.
Supports reference firmware dialect: `v` as `1` or `"1"`, `ts` as epoch seconds, ISO-8601 UTC string (`Z`), or `null` (gateway clock used with `timeSuspect: true`).
`deviceId` in payload is optional in `access`, `readings` and `ack` (defaults to topic's device id).

| Direction | Topic | Payload |
|---|---|---|
| node → gateway | `…/nodes/{deviceId}/heartbeat` | `{v, deviceId, ts, fw?, mode?, color?}`: `mode`/`color` report what the door shows; if they differ from the latest configuration the gateway re-sends `mode` (at most every `EDGE_MODE_RESYNC_INTERVAL_S`, 10 s) |
| node → gateway | `…/nodes/{deviceId}/access` | `{v, deviceId?, ts, credentialType: "RFID", credential}` |
| node → gateway | `…/nodes/{deviceId}/readings` | `{v, deviceId?, ts, sensor, value, unit}` |
| node → gateway | `…/nodes/{deviceId}/ack` | `{v, deviceId?, ts, commandId, status: "OK"\|"FAILED"\|"ERROR", detail?}` |
| gateway → node | `…/nodes/{deviceId}/commands` | `{v, commandId, type, params, ts}` |

Sensors:
- `temperature`: −40.0 .. 85.0 °C (unit `"C"` or `"°C"`)
- `humidity`: 0.0 .. 100.0 %
- `ultrasonic`: 2.0 .. 400.0 cm

Command types:
- `access_result`: `{"result": "GRANTED"|"DENIED", "reason": "...", "message": "...", "buzzer": "...", "lockMs": 5000}`
- `mode`: `{"mode": "RESERVED", "color": "#00FF88"}` or `{"mode": "FREE_ACCESS"}`
- `rgb`: `{"color": "#00FF88"}` (rejected with `FAILED "not reserved"` if in `FREE_ACCESS`)
- `unlock`, `display`, `buzzer`, `alert`, `maintenance`, `time_sync`

## Git workflow

GitFlow with Conventional Commits in English and Milestone Suffix:
- `main` holds released versions only (tagged `vMAJOR.MINOR.PATCH`).
- `develop` is the integration branch.
- Feature branches: `feature/<HITO>-<slug-in-english>` merged `--no-ff` into `develop`.
- Commits: `<type>(<scope>): <description> (H1)`.
