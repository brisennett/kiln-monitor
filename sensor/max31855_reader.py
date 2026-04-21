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
        self._spi = spi
        self._cs = cs
        self._sensor = adafruit_max31855.MAX31855(spi, cs)

    def read_sample(self) -> TemperatureSample:
        timestamp = datetime.now(timezone.utc)

        try:
            temp_c = float(self._sensor.temperature)
            cold_junction_c = float(self._sensor.reference_temperature)
        except RuntimeError as exc:
            raw_frame = self._read_raw_frame()
            raise self._build_fault_error(exc, raw_frame) from exc
        except Exception as exc:
            raw_frame = self._read_raw_frame()
            detail = f"read failed: {exc}"
            raise SensorReadError(
                detail,
                raw_frame_hex=self._format_raw_frame(raw_frame),
                fault_bits_hex=self._format_fault_bits(raw_frame),
                fault_flags=self._format_fault_flags(raw_frame),
            ) from exc

        temp_f = (temp_c * 9.0 / 5.0) + 32.0
        return TemperatureSample(
            timestamp=timestamp,
            temp_c=temp_c,
            temp_f=temp_f,
            status="OK",
            cold_junction_c=cold_junction_c,
        )

    def _build_fault_error(self, exc: RuntimeError, raw_frame: int | None) -> SensorReadError:
        detail = self._format_fault_detail(exc)
        return SensorReadError(
            detail,
            raw_frame_hex=self._format_raw_frame(raw_frame),
            fault_bits_hex=self._format_fault_bits(raw_frame),
            fault_flags=self._format_fault_flags(raw_frame),
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

    def _read_raw_frame(self) -> int | None:
        buffer = bytearray(4)
        if not self._spi.try_lock():
            return None
        try:
            self._spi.configure(baudrate=5000000, phase=0, polarity=0)
            self._cs.value = False
            self._spi.readinto(buffer)
            self._cs.value = True
            return int.from_bytes(buffer, "big")
        except Exception:
            return None
        finally:
            try:
                self._cs.value = True
            except Exception:
                pass
            self._spi.unlock()

    @staticmethod
    def _format_raw_frame(raw_frame: int | None) -> str | None:
        if raw_frame is None:
            return None
        return f"0x{raw_frame:08X}"

    @staticmethod
    def _format_fault_bits(raw_frame: int | None) -> str | None:
        if raw_frame is None:
            return None
        return f"0x{(raw_frame & 0x7):01X}"

    @classmethod
    def _format_fault_flags(cls, raw_frame: int | None) -> str | None:
        if raw_frame is None:
            return None
        flags: list[str] = []
        if raw_frame & 0x10000:
            flags.append("fault")
        if raw_frame & 0x4:
            flags.append("open circuit")
        if raw_frame & 0x2:
            flags.append("short to ground")
        if raw_frame & 0x1:
            flags.append("short to VCC")
        if not flags:
            return "none"
        return ", ".join(flags)
