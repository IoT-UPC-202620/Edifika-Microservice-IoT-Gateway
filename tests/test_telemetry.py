import json

import pytest

from edge_gateway.clock import to_epoch
from edge_gateway.models import ActiveAlert, Outbox, SensorReading
from edge_gateway.services.errors import ValidationFailed

from .conftest import AUTH


def reading(gw, sensor, value, device_id="garden-1", unit="", ts=None):
    ts = to_epoch(gw.clock.now()) if ts is None else ts
    gw.telemetry.on_reading(device_id, {"sensor": sensor, "value": value, "unit": unit, "ts": ts})
    gw.clock.advance(seconds=10)


def events(kind):
    return [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == kind]


@pytest.fixture
def node(gw):
    return gw.devices.register("garden-1", area_id="garden", capabilities=["humidity", "ultrasonic", "oled", "buzzer"])


# --- US76: humidity -------------------------------------------------------------------
def test_valid_humidity_is_stored_and_forwarded(gw, node):
    reading(gw, "humidity", 45.0, unit="%")
    row = SensorReading.get()
    assert (row.sensor, row.value, row.valid) == ("humidity", 45.0, True)
    (event,) = events("reading")
    assert event["value"] == 45.0 and event["areaId"] == "garden" and event["unit"] == "%"


@pytest.mark.parametrize("bad", [-5.0, 100.5, None])
def test_out_of_range_humidity_is_stored_as_invalid_and_not_forwarded(gw, node, bad):
    reading(gw, "humidity", bad)
    assert SensorReading.get().valid is False
    assert events("reading") == []


def test_three_invalid_readings_in_a_row_flag_a_sensor_failure(gw, node):
    for _ in range(2):
        reading(gw, "humidity", 999.0)
    assert events("alert") == []
    reading(gw, "humidity", 999.0)
    reading(gw, "humidity", 999.0)
    alerts = events("alert")
    assert [a["alert"] for a in alerts] == ["sensor_fault_humidity"]  # raised once
    assert alerts[0]["message"] == "Possible sensor failure"


def test_a_valid_reading_clears_the_sensor_failure(gw, node):
    for _ in range(3):
        reading(gw, "humidity", 999.0)
    reading(gw, "humidity", 50.0)
    assert [a["state"] for a in events("alert")] == ["raised", "cleared"]


def test_low_humidity_raises_one_event_with_hysteresis(gw, node):
    reading(gw, "humidity", 28.0)
    reading(gw, "humidity", 27.0)  # still low: no second event
    reading(gw, "humidity", 32.0)  # above the threshold but inside the hysteresis band: stays raised
    assert [(a["alert"], a["state"]) for a in events("alert")] == [("humidity_low", "raised")]
    reading(gw, "humidity", 36.0)
    assert [a["state"] for a in events("alert")] == ["raised", "cleared"]


def test_duplicate_measurement_is_ignored(gw, node):
    ts = to_epoch(gw.clock.now())
    reading(gw, "humidity", 40.0, ts=ts)
    reading(gw, "humidity", 40.0, ts=ts)
    assert SensorReading.select().count() == 1 and len(events("reading")) == 1


def test_node_with_a_wrong_clock_is_timestamped_with_gateway_time(gw, node):
    reading(gw, "humidity", 40.0, ts=0)
    assert events("reading")[0]["timeSuspect"] is True
    assert SensorReading.get().captured_at.year == 2026


def test_readings_from_unregistered_devices_are_ignored(gw):
    reading(gw, "humidity", 40.0, device_id="ghost")
    assert SensorReading.select().count() == 0


def test_node_in_maintenance_does_not_produce_readings(gw, node):
    node.status = "MAINTENANCE"
    node.save()
    reading(gw, "humidity", 10.0)
    assert SensorReading.select().count() == 0 and Outbox.select().count() == 1  # only the registration event


# --- US77: ultrasonic water level -----------------------------------------------------
def test_distance_is_converted_to_a_level_percentage(gw, node):
    reading(gw, "ultrasonic", 40.0, unit="cm")  # tank height 100 cm -> 60 % full
    assert SensorReading.get().derived == pytest.approx(60.0)
    assert events("reading")[0]["levelPct"] == 60.0


def test_level_uses_the_calibrated_tank_height(gw, node):
    gw.devices.update_settings("garden-1", {"tank_height_cm": 200})
    reading(gw, "ultrasonic", 50.0)
    assert SensorReading.get().derived == pytest.approx(75.0)


@pytest.mark.parametrize("bad", [1.0, 401.0, None])
def test_distance_outside_the_sensor_range_keeps_the_last_valid_level(gw, node, bad):
    reading(gw, "ultrasonic", 40.0)
    reading(gw, "ultrasonic", bad)
    valid = SensorReading.select().where(SensorReading.valid == True).count()  # noqa: E712
    assert valid == 1 and SensorReading.select().count() == 2
    assert len(events("reading")) == 1


def test_isolated_spike_is_smoothed_by_the_median(gw, node):
    for distance in (40.0, 41.0, 40.0, 42.0):
        reading(gw, "ultrasonic", distance)
    reading(gw, "ultrasonic", 300.0)  # spike
    last = SensorReading.select().order_by(SensorReading.id.desc()).get()
    assert last.derived == pytest.approx(59.0, abs=1.0)  # median stays near 41 cm
    assert events("alert") == []


def test_level_is_clamped_between_zero_and_one_hundred(gw, node):
    gw.devices.update_settings("garden-1", {"tank_height_cm": 50})
    reading(gw, "ultrasonic", 300.0)
    assert SensorReading.get().derived == 0.0


# --- US78: critical level alert -------------------------------------------------------
def test_critical_level_alerts_locally_and_in_the_cloud(gw, node, publisher):
    reading(gw, "ultrasonic", 90.0)  # 10 % full
    (alert,) = events("alert")
    assert alert["alert"] == "water_level_critical" and alert["state"] == "raised" and alert["levelPct"] == 10.0
    (command,) = publisher.commands("garden-1", "alert")
    assert command["params"] == {
        "kind": "water_level_critical", "active": True, "message": "Critical water level", "buzzer": "alert"
    }


def test_critical_alert_is_not_repeated_while_the_level_stays_low(gw, node, publisher):
    for _ in range(3):
        reading(gw, "ultrasonic", 90.0)
    assert len(events("alert")) == 1 and len(publisher.commands(type_="alert")) == 1


def test_alert_clears_only_after_the_hysteresis_margin(gw, node, publisher):
    reading(gw, "ultrasonic", 90.0)
    for _ in range(5):  # flush the median window with healthy values
        reading(gw, "ultrasonic", 80.0)  # 20 % = critical + hysteresis
    states = [a["state"] for a in events("alert")]
    assert states == ["raised", "cleared"]
    assert publisher.commands(type_="alert")[-1]["params"] == {"kind": "water_level_critical", "active": False}


def test_level_just_above_critical_does_not_clear_the_alert(gw, node):
    reading(gw, "ultrasonic", 90.0)
    for _ in range(5):
        reading(gw, "ultrasonic", 83.0)  # 17 %: above 15 but inside the 5 % band
    assert [a["state"] for a in events("alert")] == ["raised"]


def test_critical_alert_is_queued_even_when_the_broker_is_down(gw, node, publisher):
    publisher.fail = True
    reading(gw, "ultrasonic", 90.0)
    assert [a["state"] for a in events("alert")] == ["raised"]
    assert ActiveAlert.select().count() == 1


# --- US92: calibration ----------------------------------------------------------------
def test_calibration_changes_the_thresholds_used(gw, node):
    gw.devices.update_settings("garden-1", {"humidity_low_pct": 50})
    reading(gw, "humidity", 45.0)
    assert [a["alert"] for a in events("alert")] == ["humidity_low"]


@pytest.mark.parametrize("patch", [{"tank_height_cm": 5}, {"humidity_low_pct": 120}, {"unknown": 1}])
def test_invalid_calibration_is_rejected(gw, node, patch):
    with pytest.raises(ValidationFailed):
        gw.devices.update_settings("garden-1", patch)


def test_api_calibrates_a_device_and_lists_readings(gw, node, client):
    response = client.patch("/api/v1/devices/garden-1/settings", headers=AUTH, json={"tank_height_cm": 150})
    assert response.status_code == 200 and response.get_json()["settings"]["tank_height_cm"] == 150
    assert client.patch("/api/v1/devices/garden-1/settings", headers=AUTH, json={"tank_height_cm": 1}).status_code == 422
    assert client.patch("/api/v1/devices/nope/settings", headers=AUTH, json={"tank_height_cm": 150}).status_code == 404
    reading(gw, "humidity", 41.0, unit="%")
    reading(gw, "humidity", 999.0)
    rows = client.get("/api/v1/readings?deviceId=garden-1&sensor=humidity", headers=AUTH).get_json()
    assert [r["value"] for r in rows] == [41.0]
    everything = client.get("/api/v1/readings?includeInvalid=true", headers=AUTH).get_json()
    assert len(everything) == 2
    assert client.get("/api/v1/readings").status_code == 401


# --- Task 7: temperature --------------------------------------------------------------
def test_temperature_reading_is_forwarded(gw):
    pfx = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01", capabilities=["temperature"])
    from .helpers import msg

    topic, raw = msg(pfx, "esp32-acc-01", "readings", sensor="temperature", value=24.5, unit="°C")
    assert gw.router.handle(topic, raw) is True
    p = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "reading"][-1]
    assert (p["sensor"], p["value"], p["unit"]) == ("temperature", 24.5, "°C")


def test_temperature_out_of_range_is_invalid_and_three_raise_fault(gw):
    pfx = gw.config.mqtt_topic_prefix
    gw.devices.register("esp32-acc-01", capabilities=["temperature"])
    from .helpers import msg

    for _ in range(3):
        topic, raw = msg(pfx, "esp32-acc-01", "readings", sensor="temperature", value=120.0, unit="°C")
        gw.router.handle(topic, raw)
    alerts = [json.loads(e.payload) for e in Outbox.select().order_by(Outbox.id) if e.kind == "alert"]
    assert any(a.get("alert") == "sensor_fault_temperature" and a.get("state") == "raised" for a in alerts)
