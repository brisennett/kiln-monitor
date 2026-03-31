import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DATABASE_PATH = Path(os.getenv("KILN_MONITOR_DB_PATH", DATA_DIR / "kiln_monitor.db"))
APP_LOG_PATH = Path(os.getenv("KILN_MONITOR_LOG_PATH", LOG_DIR / "kiln_monitor.log"))

# Sensor polling cadence. A 2-second default is responsive enough for a kiln
# while keeping storage growth and sensor bus traffic modest.
READ_INTERVAL_SECONDS = float(os.getenv("KILN_MONITOR_READ_INTERVAL_SECONDS", "2.0"))

# Consecutive read failures before the monitor increases log severity.
ERROR_STREAK_WARNING_THRESHOLD = int(os.getenv("KILN_MONITOR_ERROR_STREAK_WARNING_THRESHOLD", "3"))

# Thermocouple configuration.
THERMOCOUPLE_TYPE = os.getenv("KILN_MONITOR_THERMOCOUPLE_TYPE", "K").upper()

# Named board pin used for MAX31856 chip select.
MAX31856_CS_PIN = os.getenv("KILN_MONITOR_MAX31856_CS_PIN", "D5").upper()

# SQLite durability mode. FULL is safer for an appliance-style logger where write
# rate is low and preserving recent samples across power loss matters.
SQLITE_SYNCHRONOUS_MODE = os.getenv("KILN_MONITOR_SQLITE_SYNCHRONOUS_MODE", "FULL").upper()

# Print status to the console every N successful samples.
STATUS_EVERY_N_SAMPLES = int(os.getenv("KILN_MONITOR_STATUS_EVERY_N_SAMPLES", "1"))
