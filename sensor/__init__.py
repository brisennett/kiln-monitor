"""Sensor access package for kiln monitoring."""
from config import SENSOR_MODEL
from sensor.common import SensorReadError, TemperatureSample
from sensor.max31855_reader import Max31855Reader
from sensor.max31856_reader import Max31856Reader


def build_sensor_reader():
    if SENSOR_MODEL == "MAX31855":
        return Max31855Reader()
    if SENSOR_MODEL == "MAX31856":
        return Max31856Reader()
    raise ValueError(f"Unsupported sensor model: {SENSOR_MODEL}")


__all__ = [
    "SensorReadError",
    "TemperatureSample",
    "build_sensor_reader",
]
