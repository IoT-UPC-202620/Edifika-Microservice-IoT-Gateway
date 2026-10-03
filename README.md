# Edifika Edge Gateway

On-premise gateway of the Edifika smart-building platform. It sits between the **ESP32 nodes**
(RFID reader, electric lock, OLED, buzzer, humidity and ultrasonic sensors) and the **cloud
backend**, and it keeps working when the internet is down.

```
 ESP32 nodes ──MQTT──▶  Mosquitto  ──▶  Edge Gateway  ──HTTPS──▶  Cloud backend
 (sensors, RFID,        (local          Flask · Peewee ·          (credentials, reservations,
  lock, OLED, buzzer)    broker)         SQLite · paho-mqtt        analytics, notifications)
```

What it does:

- **Decides access locally.** A card read is resolved from a cached copy of credentials, reservations
  and area schedules, so doors keep opening (or denying) during an outage.
- **Answers the node** with what to do: unlock for N ms, message for the OLED, buzzer pattern.
- **Validates sensor readings**, converts ultrasonic distance into a water-level percentage, and raises
  local alerts (low humidity, critical water level, sensor failure) once each, with hysteresis.
- **Transports everything to the backend** through a durable outbox: batches, retries with capped
  backoff, idempotent event ids, chronological order, nothing lost during an outage.
- **Keeps the cache in sync** with the cloud (pull on a schedule, push endpoint) and tells operators
  when it is stale.
- **Tracks the nodes**: registry, heartbeat, offline detection, clock synchronisation, remote
  commands with acknowledgement and a maintenance mode that fully deactivates a node.

Stack (fixed by the course statement for Edge Services): Python, Flask, Peewee ORM, SQLite.
Also: flask-smorest (OpenAPI/Swagger), marshmallow, paho-mqtt, gunicorn.

## Run the stack (Docker Compose)

```bash
docker compose up --build                  # broker + gateway + fake cloud
docker compose --profile sim up --build    # ... plus two virtual ESP32 nodes
```

| Service | Port | Purpose |
|---|---|---|
| `edge-gateway` | 8000 | REST API · Swagger UI at `http://localhost:8000/docs` · `/health` |
| `mock-cloud` | 9000 | Fake backend: `GET /events` shows what the gateway delivered |
| `mosquitto` | 1883 | Local MQTT broker |

Defaults work out of the box (`SERVICE_TOKEN=dev-service-token`); copy `.env.example` to `.env` to change them.

Try it (with the `sim` profile running):

```bash
H="Authorization: Bearer dev-service-token"

# present a card at the virtual door (exit code 0 = granted)
docker compose run --rm sim-door python -m simulator tap --device esp32-door-01 --uid 04A1B2C3

# what the gateway decided, and what the "cloud" received
curl -H "$H" localhost:8000/api/v1/access-attempts
curl "localhost:9000/events?kind=access_attempt"

# open the lock remotely, then put the node in maintenance
curl -X POST -H "$H" -H 'content-type: application/json' localhost:8000/api/v1/devices/esp32-door-01/commands -d '{"type":"unlock"}'
curl -X PUT  -H "$H" -H 'content-type: application/json' localhost:8000/api/v1/devices/esp32-door-01/maintenance -d '{"enabled":true,"durationMinutes":30}'

# simulate an internet outage, tap a card, restore, and watch the backlog drain
curl -X POST localhost:9000/admin/outage -H 'content-type: application/json' -d '{"down":true}'
curl -H "$H" localhost:8000/api/v1/status          # outbox.pending grows
curl -X POST localhost:9000/admin/outage -H 'content-type: application/json' -d '{"down":false}'
```

## Run locally (without Docker)

```bash
python -m venv .venv && .venv/Scripts/activate        # source .venv/bin/activate on Linux/macOS
pip install -r requirements-dev.txt
pytest                                                 # 188 tests, no broker or network needed

export EDGE_SERVICE_TOKEN=dev-service-token EDGE_MQTT_HOST=localhost
python main.py                                         # needs an MQTT broker on localhost:1883
```

## Configuration

Environment variables, all prefixed `EDGE_` (see `edge_gateway/config.py` for every option).
`EDGE_SERVICE_TOKEN` is required; the service refuses to start without it.

| Variable | Default | Meaning |
|---|---|---|
| `EDGE_GATEWAY_ID` | `edge-gateway-01` | Identity sent to the backend |
| `EDGE_SERVICE_TOKEN` | — | Bearer token for the local REST API |
| `EDGE_DATABASE_PATH` | `data/edge-gateway.db` | SQLite file (a volume in Docker) |
| `EDGE_MQTT_HOST` / `_PORT` | `localhost` / `1883` | Local broker |
| `EDGE_CLOUD_BASE_URL` / `EDGE_CLOUD_TOKEN` | empty | Backend; empty keeps events queued |
| `EDGE_HEARTBEAT_TIMEOUT_S` | `30` | Silence before a node is marked OFFLINE |
| `EDGE_LOCK_OPEN_MS` | `5000` | How long the lock stays energised |
| `EDGE_TIMEZONE` | `America/Lima` | Used for area schedules |

## MQTT contract (schema version 1)

Prefix `edifika/v1`. Every message is JSON with `v` (schema version) and `ts` (node clock, epoch seconds).
Unknown fields are ignored so firmware can add data; a different `v` is rejected and reported once.

| Direction | Topic | Payload |
|---|---|---|
| node → gateway | `…/nodes/{deviceId}/heartbeat` | `{v, deviceId, ts, fw?}` |
| node → gateway | `…/nodes/{deviceId}/access` | `{v, deviceId, ts, credentialType: "RFID", credential}` |
| node → gateway | `…/nodes/{deviceId}/readings` | `{v, deviceId, ts, sensor: "humidity"\|"ultrasonic", value, unit}` (`value` may be `null` = no measurement) |
| node → gateway | `…/nodes/{deviceId}/ack` | `{v, deviceId, ts, commandId, status: "OK"\|"FAILED", detail?}` |
| gateway → node | `…/nodes/{deviceId}/commands` | `{v, commandId, type, params, ts}` |

Command types the firmware must handle:

| `type` | `params` | Meaning |
|---|---|---|
| `access_result` | `result, reason, message, buzzer, name?, lockMs?` | Outcome of a card read: unlock for `lockMs`, show `message`, play `buzzer` pattern |
| `unlock` | `durationMs` | Remote opening |
| `display` | `message` | Show text on the OLED |
| `buzzer` | `pattern`: `granted\|denied\|alert\|off` | Sound |
| `alert` | `kind, active, message?, buzzer?` | Local warning (e.g. critical water level) on/off |
| `maintenance` | `active, message?` | Node deactivated / back in service |
| `time_sync` | `epoch, timezone, driftSeconds` | Set the node clock |

The node must answer every command that carries a `commandId` with an `ack`. Display texts are in English
(course default language); `es_419` can be added on the firmware side.

## Edge ⇄ Backend contract

All calls carry `Authorization: Bearer <EDGE_CLOUD_TOKEN>` and `X-Gateway-Id`.

- `POST {cloud}/api/v1/edge/events` — batch of `{eventId, kind, occurredAt, payload}`; answers
  `{"accepted": [eventIds]}`. The backend **must deduplicate by `eventId`** (delivery is at-least-once).
  Kinds: `access_attempt`, `reading`, `alert`, `device_status`, `command_result`, `credential_changed`.
  A `5xx`, `401/403`, `408` or `429` means "retry later"; any other `4xx` is a permanent rejection
  (the event is dropped after 5 attempts so it cannot block the queue).
- `GET {cloud}/api/v1/edge/sync?since={version}` — credentials, reservation windows and area schedules
  (`full: true` for a snapshot). The same body can be pushed to `POST /api/v1/sync` on the gateway.

`mock_cloud/app.py` implements both endpoints and is the reference for the payloads.

## REST API (local)

Swagger UI at `/docs`, spec at `/openapi.json`. Everything except `/health` needs the bearer token.

| Area | Endpoints |
|---|---|
| Nodes | `POST/GET /api/v1/devices` · `GET /devices/{id}` · `PATCH /devices/{id}/settings` (calibration) · `PUT /devices/{id}/maintenance` · `POST /devices/{id}/commands` |
| Credentials | `POST/GET /api/v1/credentials` (manual registration, works offline) · `PATCH /credentials/{uid}/status` |
| Logs | `GET /api/v1/access-attempts` (credentials masked) · `GET /api/v1/readings` |
| Operation | `GET /api/v1/status` · `GET /api/v1/outbox` · `POST /api/v1/sync` |

Errors share one shape: `{"code": 409, "status": "Conflict", "message": "..."}` (validation errors, 422, carry an `errors` object instead of `message`).

## Design notes

- **Offline first.** Decisions never call the cloud; everything that must reach the backend goes
  through the SQLite outbox. A record and its outbox event are written in one transaction.
- **One process on purpose.** gunicorn runs a single worker (with threads) because the MQTT client and the
  two schedulers (local checks, cloud I/O) must exist exactly once. Cloud calls run in their own scheduler
  so a slow backend never delays heartbeat checks.
- **Maintenance means deactivated.** No cards, readings, remote commands or alerts for that node until
  maintenance ends (manually or when its time expires).
- **Alerts are stateful.** Each condition is announced when raised and when cleared, never per reading.
- **Timestamps** are UTC ISO 8601 on the wire; node clocks that are far off are replaced by gateway time
  and flagged `timeSuspect`.

## Source layout

```
main.py                    entry point (gunicorn main:app)
edge_gateway/
  config.py  gateway.py    settings · composition root and background schedulers
  mqtt/                    contract (schemas), router, paho bridge
  services/                access, devices, telemetry, commands, remote, outbox, sync, alerts, credentials
  api/                     REST blueprints and schemas
  models.py  db.py         Peewee models · SQLite setup (WAL)
mock_cloud/                fake backend for local runs
simulator/                 virtual ESP32 nodes (seed · run · tap)
tests/                     pytest suite
```

## Git workflow

GitFlow with Conventional Commits and Semantic Versioning.

- `main` holds released versions only (tagged `vMAJOR.MINOR.PATCH`); `develop` is the integration branch.
- `feature/<story-id>-<short-name>` branches from `develop` and merges back with `--no-ff`
  (for example `feature/US71-access-control`).
- `release/<version>` branches from `develop`, merges into `main` and back into `develop`;
  `hotfix/<name>` branches from `main`.
- Commit messages: `feat(access): …`, `fix(mqtt): …`, `docs: …`, `test: …`, `chore: …`.

## User story coverage

| Done | Stories |
|---|---|
| Gateway side implemented and tested | US71 US72 US75 US76 US77 US78 US79 US80 US81 US82 US83 US85 US87 US89 US92 US93 · TS23 TS24 TS25 TS28 TS31 TS32 TS33 TS34 |
| Gateway provides the data; behaviour lives in the firmware | US73 (buzzer) US74 (OLED) US84 (clock on OLED) · TS26 |
| Not started | US86 (enrol card from the reader) US88 (end-of-reservation notice) US90 US91 TS30 (mobile credentials) TS27 (broker auth and TLS) TS29 (OTA) |

Known limitations: the broker allows anonymous clients (lab setup, see `mosquitto/mosquitto.conf`);
mobile credentials are rejected as unsupported; the REST API uses one shared bearer token.
