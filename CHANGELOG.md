# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-02

First working version of the Edge Gateway.

### Added
- Flask application with health endpoint, Swagger UI and bearer-token security (TS23).
- SQLite persistence with Peewee in WAL mode and an outbound event queue (TS25).
- Versioned MQTT contract, message router, paho bridge and acknowledged commands (TS24).
- Node registry, heartbeat tracking, offline detection and clock synchronisation (US79, US80, US85).
- Local RFID access decisions with lock, buzzer and OLED feedback, area schedules and manual
  card registration that works offline (US71, US72, US73, US74, US75, US87).
- Humidity and ultrasonic readings, water-level conversion, local alerts and calibration
  (US76, US77, US78, US92).
- Cloud forwarding with batching, backoff and idempotent events; cache synchronisation by pull and
  push; local status endpoints (TS33, US75, US81, US82, US93).
- Remote commands and maintenance mode (US83, US89).
- Docker Compose stack, node simulator and mock backend (TS31, TS32).
- Automated test suite (TS34).

### Not included yet
Enrolment from the reader (US86), end-of-reservation notice (US88), mobile credentials
(US90, US91, TS30), broker authentication and TLS (TS27) and OTA updates (TS29).
