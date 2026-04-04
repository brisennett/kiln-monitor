from __future__ import annotations

from datetime import datetime, timezone

import board
import digitalio
from adafruit_max31856 import MAX31856, ThermocoupleType

from config import SPI_CS_PIN, THERMOCOUPLE_TYPE
from sensor.common import SensorReadError, TemperatureSample, resolve_board_pin


THERMOCOUPLE_TYPES = {
    "B": ThermocoupleType.B,
    "E": ThermocoupleType.E,
    "J": ThermocoupleType.J,
    "K": ThermocoupleType.K,
    "N": ThermocoupleType.N,
    "R": ThermocoupleType.R,
    "S": ThermocoupleType.S,
    "T": ThermocoupleType.T,
}


class Max31856Reader:
    """Thin wrapper around the MAX31856 driver with fault-aware reads."""

    def __init__(self, cs_pin_name: str = SPI_CS_PIN, thermocouple_type: str = THERMOCOUPLE_TYPE) -> None:
        if thermocouple_type not in THERMOCOUPLE_TYPES:
            raise ValueError(f"Unsupported thermocouple type: {thermocouple_type}")

        spi = board.SPI()
        cs = digitalio.DigitalInOut(resolve_board_pin(cs_pin_name))
        self._sensor = MAX31856(spi, cs, thermocouple_type=THERMOCOUPLE_TYPES[thermocouple_type])

    def read_sample(self) -> TemperatureSample:
        timestamp = datetime.now(timezone.utc)

        try:
            temp_c = float(self._sensor.temperature)
            self._raise_on_fault()
        except Exception as exc:
            detail = self._format_fault_detail(exc)
            raise SensorReadError(detail) from exc

        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        sample = TemperatureSample(
            timestamp=timestamp,
            temp_c=temp_c,
            temp_f=temp_f,
            status="OK",
        )
        return sample

    def _raise_on_fault(self) -> None:
        fault = self._sensor.fault
        if not fault:
            return

        if isinstance(fault, dict):
            fault_messages = [
                self._format_fault_name(fault_name)
                for fault_name, is_active in fault.items()
                if is_active
            ]
            if fault_messages:
                raise SensorReadError(", ".join(fault_messages))
            return

        fault_messages = []
        if fault & 0x01:
            fault_messages.append("open circuit")
        if fault & 0x02:
            fault_messages.append("short to ground")
        if fault & 0x04:
            fault_messages.append("short to VCC")
        if fault & 0x08:
            fault_messages.append("thermocouple out of range")
        if fault & 0x10:
            fault_messages.append("cold junction out of range")
        if fault & 0x20:
            fault_messages.append("thermocouple low")
        if fault & 0x40:
            fault_messages.append("thermocouple high")
        if fault & 0x80:
            fault_messages.append("cold junction high/low")

        detail = ", ".join(fault_messages) if fault_messages else f"fault register 0x{fault:02X}"
        raise SensorReadError(detail)

    @staticmethod
    def _format_fault_detail(exc: Exception) -> str:
        if isinstance(exc, SensorReadError):
            return str(exc)
        return f"read failed: {exc}"

    @staticmethod
    def _format_fault_name(fault_name: str) -> str:
        fault_labels = {
            "cj_range": "cold junction out of range",
            "tc_range": "thermocouple out of range",
            "cj_high": "cold junction high",
            "cj_low": "cold junction low",
            "tc_high": "thermocouple high",
            "tc_low": "thermocouple low",
            "voltage": "over/under voltage",
            "open_tc": "open circuit",
        }
        return fault_labels.get(fault_name, fault_name)
