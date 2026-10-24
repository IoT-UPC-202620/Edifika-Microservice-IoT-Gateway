import pytest
from peewee import IntegrityError

from edge_gateway import create_app
from edge_gateway.db import database
from edge_gateway.models import AccessAttempt, Credential, Outbox
from edge_gateway.services import outbox as ob


@pytest.fixture
def gateway(app):
    return app.extensions["gateway"]


def test_health_includes_database_probe(client):
    body = client.get("/health").get_json()
    assert body["checks"]["database"]["ok"] is True


def test_database_uses_wal_mode(app):
    with database.connection_context():
        mode = database.execute_sql("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_data_survives_a_restart(config, gateway, app):
    from datetime import datetime

    with database.connection_context():
        Credential.create(uid="AA11", resident_id="r1", updated_at=datetime(2026, 1, 1))
    create_app(config)  # simulates a service restart on the same file
    with database.connection_context():
        assert Credential.get_by_id("AA11").resident_id == "r1"


def test_attempt_and_event_are_written_atomically(gateway):
    from datetime import datetime

    now = datetime(2026, 1, 1)
    with database.connection_context():
        with pytest.raises(RuntimeError):
            with database.atomic():
                AccessAttempt.create(
                    device_id="d1", credential="AA", result="GRANTED", reason="ok", occurred_at=now
                )
                gateway.outbox.enqueue(ob.ACCESS_ATTEMPT, {"x": 1})
                raise RuntimeError("boom")
        assert AccessAttempt.select().count() == 0
        assert Outbox.select().count() == 0


def test_outbox_event_ids_are_unique(gateway):
    with database.connection_context():
        a = gateway.outbox.enqueue(ob.ALERT, {"a": 1})
        b = gateway.outbox.enqueue(ob.ALERT, {"a": 1})
        assert a.event_id != b.event_id
        with pytest.raises(IntegrityError):
            Outbox.create(event_id=a.event_id, kind="x", payload="{}", occurred_at=a.occurred_at, created_at=a.created_at)


def test_full_outbox_drops_oldest_telemetry_but_keeps_critical_events(config):
    from dataclasses import replace

    small = replace(config, outbox_max_rows=3)
    app = create_app(small)
    gw = app.extensions["gateway"]
    with database.connection_context():
        gw.outbox.enqueue(ob.ACCESS_ATTEMPT, {"n": "access"})
        gw.outbox.enqueue(ob.READING, {"n": "r1"})
        gw.outbox.enqueue(ob.READING, {"n": "r2"})
        gw.outbox.enqueue(ob.ALERT, {"n": "alert"})
        kinds = [row.kind for row in Outbox.select().order_by(Outbox.id)]
    assert kinds == [ob.ACCESS_ATTEMPT, ob.READING, ob.ALERT]


def test_outbox_never_drops_critical_events_even_when_over_capacity(config):
    from dataclasses import replace

    app = create_app(replace(config, outbox_max_rows=2))
    gw = app.extensions["gateway"]
    with database.connection_context():
        for i in range(4):
            gw.outbox.enqueue(ob.ACCESS_ATTEMPT, {"n": i})
        assert gw.outbox.pending_count() == 4


def test_v010_database_is_migrated_in_place(tmp_path):
    from peewee import SqliteDatabase
    from edge_gateway.db import init_database
    from edge_gateway.models import Device

    db_path = str(tmp_path / "v010.db")
    raw_db = SqliteDatabase(db_path)
    raw_db.connect()
    raw_db.execute_sql(
        """
        CREATE TABLE "device" (
            "device_id" VARCHAR(255) NOT NULL PRIMARY KEY,
            "area_id" VARCHAR(255),
            "location" VARCHAR(255) NOT NULL,
            "capabilities" TEXT NOT NULL,
            "status" VARCHAR(255) NOT NULL,
            "last_seen" DATETIME,
            "firmware" VARCHAR(255),
            "maintenance_since" DATETIME,
            "maintenance_until" DATETIME,
            "config" TEXT NOT NULL
        )
        """
    )
    raw_db.execute_sql(
        """
        INSERT INTO "device" ("device_id", "location", "capabilities", "status", "config")
        VALUES ('esp32-old', 'lobby', '["rfid","lock"]', 'ACTIVE', '{}')
        """
    )
    raw_db.close()

    init_database(db_path)
    with database.connection_context():
        d = Device.get_by_id("esp32-old")
        assert d.location == "lobby"
        assert d.current_mode == "FREE_ACCESS"
        assert d.managed == "local"
        assert d.name == ""
        assert d.led_color is None

