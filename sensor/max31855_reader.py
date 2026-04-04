from __future__ import annotations

from datetime import datetime, timezone

import adafruit_max31855
import board
import digitalio

from config import SPI_CS_PIN
from sensor.common import SensorReadError, TemperatureSample, resolve_board_pin


class Max31855Reader:
    """MAX31855 reader for K-type thermocouple measurements."""

    def __init__(self, cs_pin_name: str = SPI_CS_PIN) -> None:
        spi = board.SPI()
        cs = digitalio.DigitalInOut(resolve_board_pin(cs_pin_name))
        self._sensor = adafruit_max31855.MAX31855(spi, cs)

    def read_sample(self) -> TemperatureSample:
        timestamp = datetime.now(timezone.utc)

        try:
            temp_c = float(self._sensor.temperature)
            cold_junction_c = float(self._sensor.reference_temperature)
        except RuntimeError as exc:
            raise SensorReadError(self._format_fault_detail(exc)) from exc
        except Exception as exc:
            raise SensorReadError(f"read failed: {exc}") from exc

        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        return TemperatureSample(
            timestamp=timestamp,
            temp_c=temp_c,
            temp_f=temp_f,
            status="OK",
            cold_junction_c=cold_junction_c,
        )

    @staticmethod
    def _format_fault_detail(exc: RuntimeError) -> str:
        fault_text = str(exc).strip().lower()
        if "thermocouple not connected" in fault_text:
            return "open circuit"
        if "short circuit to ground" in fault_text:
            return "short to ground"
        if "short circuit to power" in fault_text:
            return "short to VCC"
        if "faulty reading" in fault_text:
            return "faulty reading"
        return str(exc)
