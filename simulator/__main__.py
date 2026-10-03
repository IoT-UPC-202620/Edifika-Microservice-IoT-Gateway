"""ESP32 node simulator: lets you exercise the Edge Gateway without hardware.

    python -m simulator seed                              register the demo nodes in the gateway
    python -m simulator run --device esp32-garden-01      heartbeat + sensor readings, acks commands
    python -m simulator tap --device esp32-door-01 --uid 04A1B2C3    present a card
"""

import argparse
import json
import os
import random
import sys
import threading
import time

import paho.mqtt.client as mqtt
import requests

PREFIX = os.environ.get("SIM_TOPIC_PREFIX", "edifika/v1")
HOST = os.environ.get("SIM_MQTT_HOST", "localhost")
PORT = int(os.environ.get("SIM_MQTT_PORT", "1883"))
API = os.environ.get("SIM_GATEWAY_URL", "http://localhost:8000")
TOKEN = os.environ.get("SIM_SERVICE_TOKEN", "dev-service-token")


def topic(device: str, channel: str) -> str:
    return f"{PREFIX}/nodes/{device}/{channel}"


class Node:
    def __init__(self, device: str, ack: bool = True):
        self.device = device
        self.ack = ack
        self.commands: list[dict] = []
        self.received = threading.Event()
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{device}-{os.getpid()}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def connect(self) -> None:
        for attempt in range(30):
            try:
                self.client.connect(HOST, PORT, keepalive=30)
                break
            except OSError:
                print(f"[{self.device}] broker not ready, retrying ({attempt + 1})", flush=True)
                time.sleep(2)
        else:
            sys.exit("Could not reach the MQTT broker")
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        client.subscribe(topic(self.device, "commands"), qos=1)

    def _on_message(self, client, userdata, msg):
        command = json.loads(msg.payload)
        self.commands.append(command)
        print(f"[{self.device}] <- {command['type']} {json.dumps(command['params'])}", flush=True)
        if self.ack:
            self.publish("ack", commandId=command["commandId"], status="OK")
        self.received.set()

    def publish(self, channel: str, **fields) -> None:
        body = {"v": 1, "deviceId": self.device, "ts": time.time(), **fields}
        self.client.publish(topic(self.device, channel), json.dumps(body), qos=1)


def run(args) -> None:
    node = Node(args.device, ack=not args.no_ack)
    node.connect()
    humidity, distance = 45.0, 40.0
    tick = 0
    print(f"[{args.device}] running (heartbeat every {args.interval}s)", flush=True)
    try:
        while True:
            node.publish("heartbeat", fw="sim-0.1.0")
            if tick % 2 == 0:
                humidity = min(100.0, max(0.0, humidity + random.uniform(-3, 2)))
                distance = min(120.0, max(5.0, distance + random.uniform(-1, 3)))
                node.publish("readings", sensor="humidity", value=round(humidity, 1), unit="%")
                node.publish("readings", sensor="ultrasonic", value=round(distance, 1), unit="cm")
            tick += 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        node.stop()


def tap(args) -> None:
    node = Node(args.device)
    node.connect()
    time.sleep(0.5)  # let the subscription settle
    node.publish("access", credentialType="RFID", credential=args.uid)
    print(f"[{args.device}] -> card {args.uid}", flush=True)
    if not node.received.wait(5):
        node.stop()
        sys.exit("No answer from the gateway within 5 seconds")
    answer = node.commands[0]["params"]
    print(f"[{args.device}] {answer.get('result')}: {answer.get('message')}", flush=True)
    time.sleep(0.3)
    node.stop()
    sys.exit(0 if answer.get("result") == "GRANTED" else 1)


DEMO_DEVICES = [
    {"deviceId": "esp32-door-01", "areaId": "pool", "location": "Pool entrance",
     "capabilities": ["rfid", "lock", "oled", "buzzer", "rtc"]},
    {"deviceId": "esp32-garden-01", "areaId": "garden", "location": "Garden and water tank",
     "capabilities": ["humidity", "ultrasonic", "oled", "buzzer", "rtc"]},
]


def seed(_args) -> None:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    for device in DEMO_DEVICES:
        response = requests.post(f"{API}/api/v1/devices", json=device, headers=headers, timeout=10)
        print(device["deviceId"], "->", response.status_code, flush=True)
        if response.status_code not in (201, 409):
            sys.exit(response.text)
    requests.patch(
        f"{API}/api/v1/devices/esp32-garden-01/settings",
        json={"tank_height_cm": 100, "critical_level_pct": 15, "humidity_low_pct": 30},
        headers=headers,
        timeout=10,
    ).raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(prog="simulator")
    sub = parser.add_subparsers(dest="command", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--device", required=True)
    p_run.add_argument("--interval", type=float, default=5.0)
    p_run.add_argument("--no-ack", action="store_true", help="do not confirm commands (simulates a stuck lock)")
    p_run.set_defaults(func=run)
    p_tap = sub.add_parser("tap")
    p_tap.add_argument("--device", required=True)
    p_tap.add_argument("--uid", required=True)
    p_tap.set_defaults(func=tap)
    sub.add_parser("seed").set_defaults(func=seed)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
