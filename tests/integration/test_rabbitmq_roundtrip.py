import json
import time
from datetime import datetime, timedelta, timezone

import pika
import pytest

from edge_gateway.clock import to_iso
from edge_gateway.db import database
from edge_gateway.models import DoorReservation, Outbox
from edge_gateway.services.amqp import AmqpEventPublisher


@pytest.mark.integration
def test_tap_reaches_rabbitmq_within_6_seconds(it_gateway, spy):
    t0 = time.monotonic()
    it_gateway.tap("esp32-acc-01", "04A1B2C3")

    env = None
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        try:
            candidate = spy.get(timeout=max(0.1, deadline - time.monotonic()))
            if candidate.get("eventType") == "AccessAttempt":
                env = candidate
                break
        except Exception:
            pass

    assert env is not None, "Did not receive AccessAttempt event from RabbitMQ within timeout"
    elapsed = time.monotonic() - t0
    assert elapsed <= 6.0, f"Tap to RabbitMQ took {elapsed:.2f}s (> 6s)"
    assert env["eventType"] == "AccessAttempt"
    assert env["payload"]["credential"] == "04A1B2C3"
    assert env["payload"]["result"] == "GRANTED"


@pytest.mark.integration
def test_twenty_taps_during_outage_arrive_once(it_gateway, spy, amqp_url):
    gw = it_gateway.gw
    # Temporarily switch forwarder cloud to a broken publisher pointing to an unreachable port
    broken_cfg = gw.config
    broken_pub = AmqpEventPublisher(
        broken_cfg,
        connection_factory=lambda: pika.BlockingConnection(
            pika.URLParameters("amqp://guest:guest@127.0.0.1:1/")
        ),
    )
    gw.forwarder.cloud = broken_pub

    # Perform 20 distinct taps (distinct cards so deduplication doesn't ignore them)
    for i in range(20):
        it_gateway.tap("esp32-acc-01", f"04A1B2{i:02X}")
        time.sleep(0.05)

    time.sleep(1.0)
    with database.connection_context():
        pending_count = Outbox.select().where(Outbox.sent_at.is_null()).count()
        assert pending_count >= 20

    # Switch back to the real AMQP publisher and flush
    real_pub = AmqpEventPublisher(gw.config)
    gw.forwarder.cloud = real_pub
    delivered = gw.forwarder.flush()
    assert delivered >= 20

    # Collect events from RabbitMQ spy
    event_ids = set()
    deadline = time.monotonic() + 10.0
    while len(event_ids) < 20 and time.monotonic() < deadline:
        try:
            env = spy.get(timeout=1.0)
            if env.get("eventType") == "AccessAttempt":
                event_ids.add(env["eventId"])
        except Exception:
            pass

    assert len(event_ids) == 20, f"Expected 20 distinct event IDs, got {len(event_ids)}"


@pytest.mark.integration
def test_cloud_order_reaches_node_and_result_returns(it_gateway, mqtt_spy, spy_results, amqp_url):
    gw = it_gateway.gw
    now = datetime.now(timezone.utc)

    # Pre-create reservation so the door is RESERVED
    with database.connection_context():
        DoorReservation.delete().execute()
        DoorReservation.create(
            source_id="res-it-1",
            device_id="esp32-acc-01",
            holder_resident_id="15",
            starts_at=now - timedelta(minutes=5),
            ends_at=now + timedelta(hours=1),
        )
        # Update device current mode
        from edge_gateway.models import Device

        dev = Device.get_by_id("esp32-acc-01")
        dev.current_mode = "RESERVED"
        dev.save()

    # Publish DeviceCommandRequested to edifika.edge.commands
    command_id = "cmd-it-0123456789abcdef01234567"
    order_payload = {
        "commandId": command_id,
        "deviceCode": "esp32-acc-01",
        "type": "rgb",
        "params": {"color": "#00FF88"},
        "expiresAt": to_iso(now + timedelta(minutes=1)),
    }
    envelope = {
        "eventId": "ev-order-1",
        "eventType": "DeviceCommandRequested",
        "eventVersion": 1,
        "occurredAt": to_iso(now),
        "producer": "iot-access-management",
        "buildingId": None,
        "gatewayCode": "edge-gateway-01",
        "correlationId": None,
        "payload": order_payload,
    }

    conn = pika.BlockingConnection(pika.URLParameters(amqp_url))
    ch = conn.channel()
    ch.basic_publish(
        exchange=gw.config.amqp_commands_exchange,
        routing_key="edge-gateway-01.device-command",
        body=json.dumps(envelope),
        properties=pika.BasicProperties(content_type="application/json", delivery_mode=2),
    )
    conn.close()

    # Verify mqtt_spy sees rgb command within 3s
    t0 = time.monotonic()
    mqtt_msg = None
    while time.monotonic() - t0 < 3.0:
        try:
            topic, payload = mqtt_spy.get(timeout=0.5)
            if payload.get("type") == "rgb" and payload.get("commandId") == command_id:
                mqtt_msg = payload
                break
        except Exception:
            pass

    assert mqtt_msg is not None, "Node did not receive rgb command over MQTT in < 3s"
    assert mqtt_msg["params"]["color"] == "#00FF88"

    # Simulate node sending ack OK
    it_gateway.publish_node_msg("esp32-acc-01", "ack", commandId=command_id, status="OK")

    # Flush outbox so CommandResult reaches RabbitMQ
    time.sleep(0.5)
    gw.forwarder.flush()

    # Verify spy_results receives CommandResult status OK
    result_env = spy_results.get(timeout=5.0)
    assert result_env["eventType"] == "CommandResult"
    assert result_env["payload"]["commandId"] == command_id
    assert result_env["payload"]["status"] == "OK"
    assert result_env["correlationId"] == command_id


@pytest.mark.integration
def test_local_decision_under_500ms(it_gateway, mqtt_spy):
    # Warm-up tap with a different card to eliminate initial connection/import latency
    it_gateway.tap("esp32-acc-01", "04A1B299")
    try:
        mqtt_spy.get(timeout=2.0)
    except Exception:
        pass

    t0 = time.monotonic()
    it_gateway.tap("esp32-acc-01", "04A1B2C3")

    decision_msg = None
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            topic, payload = mqtt_spy.get(timeout=max(0.01, deadline - time.monotonic()))
            if payload.get("type") == "access_result":
                decision_msg = payload
                break
        except Exception:
            pass

    elapsed = time.monotonic() - t0
    assert decision_msg is not None, f"Local decision did not arrive within 500ms (elapsed: {elapsed:.3f}s)"
    assert elapsed < 0.5, f"Local decision took {elapsed:.3f}s (>= 500ms)"
    assert decision_msg["params"]["result"] == "GRANTED"
