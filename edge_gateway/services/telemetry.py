"""Sensor readings: validation, derived values (water level) and local alerts."""

import logging
import math
from statistics import median

from ..clock import from_epoch, to_epoch, to_iso
from ..models import SensorReading
from . import outbox as ob

logger = logging.getLogger(__name__)

VALID_RANGES = {"humidity": (0.0, 100.0), "ultrasonic": (2.0, 400.0)}  # %, cm
CLOCK_TOLERANCE_S = 300

DEFAULTS = {
    "humidity_low_pct": 30.0,
    "tank_height_cm": 100.0,  # sensor-to-bottom distance of the water tank
    "critical_level_pct": 15.0,
    "hysteresis_pct": 5.0,
}
SETTING_LIMITS = {
    "humidity_low_pct": (0.0, 100.0),
    "tank_height_cm": (10.0, 400.0),
    "critical_level_pct": (0.0, 100.0),
    "hysteresis_pct": (0.0, 50.0),
}
INVALID_STREAK_LIMIT = 3
MEDIAN_WINDOW = 5


def setting(device, name: str) -> float:
    return float(device.settings.get(name, DEFAULTS[name]))


class TelemetryService:
    def __init__(self, config, clock, devices, commands, outbox, alerts):
        self.config = config
        self.clock = clock
        self.devices = devices
        self.commands = commands
        self.outbox = outbox
        self.alerts = alerts
        self._invalid_streak: dict[tuple[str, str], int] = {}

    def on_reading(self, device_id: str, data: dict) -> None:
        device = self.devices.known_device(device_id)
        if device is None:
            return
        self.devices.touch(device)
        if device.status == "MAINTENANCE":
            logger.debug("Ignoring reading from %s (maintenance)", device_id)
            return

        sensor, value, unit = data["sensor"], data["value"], data["unit"]
        now = self.clock.now()
        captured_at, suspect = self._captured_at(data["ts"], now)
        key = (device_id, sensor)

        if not self._is_valid(sensor, value):
            self._record_invalid(device_id, sensor, value, unit, captured_at, key)
            return

        self._invalid_streak[key] = 0
        self.alerts.clear_alert(device_id, f"sensor_fault_{sensor}", {"sensor": sensor})
        if SensorReading.select().where(
            SensorReading.device_id == device_id,
            SensorReading.sensor == sensor,
            SensorReading.captured_at == captured_at,
            SensorReading.valid == True,  # noqa: E712 (peewee expression)
        ).exists():
            return  # duplicate delivery of the same measurement

        derived = None
        if sensor == "ultrasonic":
            derived = self._water_level(device, value, now)
        SensorReading.create(
            device_id=device_id, sensor=sensor, value=value, unit=unit,
            captured_at=captured_at, valid=True, derived=derived,
        )
        if sensor == "humidity":
            self._evaluate_humidity(device, value)
        else:
            self._evaluate_water_level(device, derived)

        payload = {
            "deviceId": device_id,
            "areaId": device.area_id,
            "sensor": sensor,
            "value": value,
            "unit": unit,
            "capturedAt": to_iso(captured_at),
        }
        if derived is not None:
            payload["levelPct"] = round(derived, 1)
        if suspect:
            payload["timeSuspect"] = True
        self.outbox.enqueue(ob.READING, payload, occurred_at=captured_at)

    # --- validation ---------------------------------------------------------------
    @staticmethod
    def _is_valid(sensor: str, value) -> bool:
        if value is None or isinstance(value, bool) or math.isnan(value) or math.isinf(value):
            return False
        low, high = VALID_RANGES[sensor]
        return low <= value <= high

    @staticmethod
    def _captured_at(node_ts: float, now):
        """Use the node timestamp unless its clock is far from the gateway's."""
        node_time = from_epoch(node_ts)
        if abs(to_epoch(now) - node_ts) <= CLOCK_TOLERANCE_S:
            return node_time, False
        return now, True

    def _record_invalid(self, device_id, sensor, value, unit, captured_at, key) -> None:
        SensorReading.create(
            device_id=device_id, sensor=sensor,
            value=-1.0 if value is None or not math.isfinite(value) else value,
            unit=unit, captured_at=captured_at, valid=False,
        )
        streak = self._invalid_streak.get(key, 0) + 1
        self._invalid_streak[key] = streak
        logger.warning("Invalid %s reading from %s (%s in a row)", sensor, device_id, streak)
        if streak >= INVALID_STREAK_LIMIT:
            self.alerts.raise_alert(
                device_id, f"sensor_fault_{sensor}", {"sensor": sensor, "message": "Possible sensor failure"}
            )

    # --- derived values and alerts --------------------------------------------------
    def _water_level(self, device, distance_cm: float, now) -> float:
        """Level in % from the median of the latest distances, which filters isolated spikes."""
        recent = [
            r.value
            for r in SensorReading.select(SensorReading.value)
            .where(
                SensorReading.device_id == device.device_id,
                SensorReading.sensor == "ultrasonic",
                SensorReading.valid == True,  # noqa: E712
            )
            .order_by(SensorReading.captured_at.desc())
            .limit(MEDIAN_WINDOW - 1)
        ]
        smoothed = median([distance_cm, *recent])
        height = setting(device, "tank_height_cm")
        return max(0.0, min(100.0, (height - smoothed) / height * 100.0))

    def _evaluate_humidity(self, device, humidity: float) -> None:
        threshold = setting(device, "humidity_low_pct")
        hysteresis = setting(device, "hysteresis_pct")
        details = {"value": humidity, "threshold": threshold, "areaId": device.area_id}
        if humidity < threshold:
            self.alerts.raise_alert(device.device_id, "humidity_low", details)
        elif humidity >= threshold + hysteresis:
            self.alerts.clear_alert(device.device_id, "humidity_low", details)

    def _evaluate_water_level(self, device, level: float) -> None:
        critical = setting(device, "critical_level_pct")
        hysteresis = setting(device, "hysteresis_pct")
        details = {"levelPct": round(level, 1), "threshold": critical, "areaId": device.area_id}
        if level <= critical:
            if self.alerts.raise_alert(device.device_id, "water_level_critical", details):
                self._signal_node(device, active=True)
        elif level >= critical + hysteresis:
            if self.alerts.clear_alert(device.device_id, "water_level_critical", details):
                self._signal_node(device, active=False)

    def _signal_node(self, device, active: bool) -> None:
        """Local warning on the node: message on the display and sound, or back to normal."""
        params = {"kind": "water_level_critical", "active": active}
        if active:
            params.update({"message": "Critical water level", "buzzer": "alert"})
        try:
            self.commands.send(device.device_id, "alert", params)
        except Exception:
            logger.exception("Could not signal node %s (alert is already queued)", device.device_id)
